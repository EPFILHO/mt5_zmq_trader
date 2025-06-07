# Arquivo: gui/boleta_trader_gui.py
# Versão: 1.0.9.k - Envio 5
# Objetivo: Implementa a interface de boleta de trading com abas ordenadas alfabeticamente
# e sub-abas para ordens abertas, posições pendentes e histórico de trades.
# Ajustes:
# - Código reorganizado em blocos modulares para melhor organização e manutenção.
# - Comentários detalhados adicionados para cada bloco.
# - [FIX 1] Nível de log obtido do config.ini.
# - [FIX 1, 2] Gerenciamento inteligente de abas para preservar a seleção e evitar limpeza desnecessária.
# - [FIX 1, 2] Lógica de seleção de abas aprimorada: mantém a aba atual OU foca na aba da corretora recém-registrada.
# - [FIX 3] Corrigido problema de posições não aparecerem após reinício do MT5 (reset da flag positions_requested).
# - [FIX 4] Atualização automática da tabela de posições/histórico ao receber eventos de trade via stream.

# Bloco 1 - Importações e Configuração Inicial
# Objetivo: Importar bibliotecas necessárias e configurar o logging para depuração e monitoramento.
# Este bloco define as dependências do sistema e o formato de logs para rastrear eventos e erros.
import sys
import json
import time
import asyncio
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
    QTableWidgetItem, QPushButton, QTextEdit, QLabel, QDoubleSpinBox,
    QAbstractItemView,
)
from PySide6.QtCore import Slot, Qt
import logging
from core.config_manager import ConfigManager  # [FIX 3] Importar ConfigManager

# [FIX 3] Configuração de logging para registrar eventos e erros em detalhes.
# Obtém o nível de log do config.ini.
config_for_logging = ConfigManager()
log_level_str = config_for_logging.get('General', 'log_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Bloco 2 - Definição da Classe BoletaTraderGui
# Objetivo: Inicializar a classe principal da boleta de trading, seus atributos e chamar métodos de setup.
# Este bloco serve como ponto de entrada para a interface gráfica da boleta, armazenando referências
# a configurações, gerenciadores de corretoras, roteadores ZMQ e o manipulador de mensagens.
class BoletaTraderGui(QDialog):
    def __init__(self, config, broker_manager, zmq_router, zmq_message_handler, main_window, parent=None):
        """
        Inicializa a interface gráfica da Boleta Trader.

        Args:
            config: Instância do gerenciador de configurações.
            broker_manager: Instância do gerenciador de corretoras.
            zmq_router: Instância do roteador ZMQ para comunicação com o EA.
            zmq_message_handler: Instância do manipulador de mensagens ZMQ.
            main_window: Referência à janela principal da aplicação.
            parent: Widget pai (opcional, padrão None).
        """
        super().__init__(parent)
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.zmq_message_handler = zmq_message_handler
        self.main_window = main_window

        # Dicionários para armazenar o status e modos das corretoras.
        self.broker_status = {}
        self.broker_connected = {}
        self.broker_modes = {}

        # Dicionários para rastrear solicitações de posições e tickets pendentes.
        self.positions_requested = {}
        self.pending_tickets = {}

        # [FIX 1, 2] Armazena a chave da corretora da aba atualmente selecionada.
        self.current_selected_broker_key = None

        self.setWindowTitle("Boleta Trader GUI")
        self.setGeometry(100, 100, 1100, 600)
        self.setMinimumWidth(1100)

        # Atualiza o status inicial das corretoras e configura a UI.
        self._update_broker_status_initial()
        self.setup_ui()
        self._connect_signals()

        # Popula as abas das corretoras e solicita posições iniciais.
        # [FIX 1, 2] Não passa select_key aqui, pois a seleção inicial será baseada na aba salva ou na primeira.
        self._populate_broker_tabs()
        self._request_positions_for_registered_brokers()

        logger.info("Bloco 2 - BoletaTraderGui inicializado.")

    # Bloco 3 - Configuração da Interface Gráfica (UI Setup)
    # Objetivo: Definir o layout da interface gráfica, widgets (abas, tabelas, botões, área de log)
    # e organizar a estrutura visual da GUI.
    def setup_ui(self):
        """
        Configura a interface gráfica da boleta.
        Cria o layout principal, as abas para as corretoras, a área de log e os botões de controle.
        """
        layout = QVBoxLayout(self)

        # QTabWidget para as abas das corretoras.
        self.broker_tabs = QTabWidget()
        self.broker_tabs.setStyleSheet("""
            QTabBar::tab:selected {
                font-weight: bold;
            }
        """)  # Negrito na aba selecionada
        layout.addWidget(self.broker_tabs)

        # Área de log para exibir mensagens de atividades.
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(100)
        layout.addWidget(QLabel("Log de Atividades:"))
        layout.addWidget(self.log_area)

        # Layout para os botões de controle (Atualizar e Fechar).
        control_layout = QHBoxLayout()

        # Botão para atualizar as posições.
        update_btn = QPushButton("Atualizar Agora")
        update_btn.clicked.connect(self._request_positions)
        update_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                padding: 5px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)

        # Botão para fechar a janela da boleta.
        close_btn = QPushButton("Fechar Janela")
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #ff3333;
                color: white;
                padding: 5px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #e62e2e;
            }
        """)

        control_layout.addWidget(update_btn)
        control_layout.addWidget(close_btn)
        control_layout.setAlignment(Qt.AlignCenter)
        layout.addLayout(control_layout)
        logger.debug("Bloco 3 - Interface configurada.")

    # Bloco 4 - Gerenciamento de Status e Sinais
    # Objetivo: Conectar os sinais do Qt para eventos de interface e sinais personalizados para
    # atualização de dados recebidos do EA. Lida também com a atualização inicial do status das corretoras.
    def _update_broker_status_initial(self):
        """
        Atualiza o status inicial das corretoras a partir da main_window.
        Popula os dicionários `broker_status`, `broker_modes` e `broker_connected`.
        """
        try:
            if hasattr(self.main_window, 'broker_status') and hasattr(self.main_window, 'broker_modes'):
                self.broker_status.update(self.main_window.broker_status)
                self.broker_modes.update(self.main_window.broker_modes)
                logger.debug(f"Bloco 4 - Status de registro atualizado: {self.broker_status}")
                logger.debug(f"Bloco 4 - Modos de corretoras atualizados: {self.broker_modes}")
            else:
                logger.warning("Bloco 4 - main_window não possui broker_status ou broker_modes ao iniciar.")

            connected_brokers = self.broker_manager.get_connected_brokers()
            for broker_key in self.broker_manager.get_brokers():
                self.broker_connected[broker_key] = broker_key in connected_brokers
            logger.debug(f"Bloco 4 - Status de conexão atualizado no início: {self.broker_connected}")
        except Exception as e:
            logger.error(f"Bloco 4 - Erro ao atualizar status inicial de corretoras: {str(e)}")
            self.update_log(f"Erro ao atualizar status inicial de corretoras: {str(e)}")

    def _connect_signals(self):
        """
        Conecta os sinais do ZmqMessageHandler e main_window aos slots correspondentes
        nesta classe para atualização da interface.
        """
        self.zmq_message_handler.log_message_received.connect(self.update_log)
        self.zmq_message_handler.positions_received.connect(self._update_positions)
        self.zmq_message_handler.trade_response_received.connect(self._update_trade_response)
        self.zmq_message_handler.history_trades_received.connect(self._update_history_trades)

        # Sinais da main_window para atualização de status de corretoras.
        self.main_window.broker_status_updated.connect(self._on_broker_status_updated)
        # [FIX 2] O sinal broker_connected agora passa a chave da corretora.
        self.main_window.broker_connected.connect(self._on_broker_connected)

        # Sinal do BrokerManager para notificar mudanças na lista de corretoras.
        # [FIX 1] O _populate_broker_tabs será chamado sem argumento aqui, pois a seleção será tratada internamente.
        self.broker_manager.brokers_updated.connect(self._populate_broker_tabs)

        # [FIX 1] Conecta o sinal de mudança de aba para armazenar a aba selecionada.
        self.broker_tabs.currentChanged.connect(self._on_tab_changed)

        # [FIX 4] Conecta o novo sinal de eventos de trade.
        self.zmq_message_handler.trade_event_received.connect(self._on_trade_event_received)

        logger.debug("Bloco 4 - Sinais conectados no BoletaTraderGui.")

    @Slot(dict, dict)
    def _on_broker_status_updated(self, broker_status, broker_modes):
        """
        Slot para atualizar o status e modos das corretoras quando o sinal `broker_status_updated` é emitido.
        Reordena as abas e solicita posições para corretoras recém-registradas.
        """
        try:
            # [FIX 2] Armazena o estado anterior do broker_status para identificar novas ativações.
            previous_broker_status = self.broker_status.copy()

            self.broker_status.update(broker_status)
            self.broker_modes.update(broker_modes)
            logger.debug(f"Bloco 4 - Status de corretoras atualizado: {self.broker_status}")
            logger.debug(f"Bloco 4 - Modos de corretoras atualizados: {self.broker_modes}")

            # [FIX 3] Resetar positions_requested para corretoras que ficaram offline.
            for broker_key, was_registered in previous_broker_status.items():
                is_registered_now = self.broker_status.get(broker_key, False)
                if was_registered and not is_registered_now:
                    self.positions_requested[broker_key] = False
                    logger.info(f"Bloco 4 - Corretora {broker_key} ficou offline. Resetando positions_requested.")

            # [FIX 1, 2] Chama _populate_broker_tabs sem argumento, a seleção será tratada abaixo.
            self._populate_broker_tabs()

            newly_registered_broker_key = None
            for broker_key, is_registered_now in self.broker_status.items():
                was_registered_before = previous_broker_status.get(broker_key, False)
                # [FIX 2] Se a corretora acabou de se registrar (passou de False para True)
                if is_registered_now and not was_registered_before:
                    newly_registered_broker_key = broker_key
                    logger.info(f"Bloco 4 - Corretora {broker_key} recém-registrada.")
                    break  # Assume que apenas uma corretora será registrada por vez para focar nela.

            # [FIX 2] Se uma corretora acabou de se registrar, tenta selecioná-la.
            if newly_registered_broker_key:
                index_to_set = -1
                for i in range(self.broker_tabs.count()):
                    tab_text = self.broker_tabs.tabText(i)
                    if tab_text.startswith(newly_registered_broker_key):
                        index_to_set = i
                        break
                if index_to_set != -1:
                    self.broker_tabs.setCurrentIndex(index_to_set)
                    logger.info(f"Bloco 4 - Aba para {newly_registered_broker_key} selecionada (recém-registrada).")
                else:
                    logger.warning(
                        f"Bloco 4 - Não foi possível selecionar a aba para {newly_registered_broker_key} (recém-registrada, mas não encontrada).")

            # Solicita posições para corretoras recém-registradas (se ainda não solicitadas)
            for broker_key in self.broker_status:
                if (self.broker_connected.get(broker_key, False) and
                        self.broker_status[broker_key] and not self.positions_requested.get(broker_key, False)):
                    self._request_positions_for_broker(broker_key)
                    self.positions_requested[broker_key] = True
                    self.update_log(f"Solicitando posições para {broker_key}...")

            logger.info(f"Bloco 4 - Status de corretoras atualizado: {self.broker_status}")
        except Exception as e:
            logger.error(f"Bloco 4 - Erro ao atualizar status de corretoras: {str(e)}")
            self.update_log(f"Erro ao atualizar status de corretoras: {str(e)}")

    # [FIX 2] O sinal broker_connected agora passa a chave da corretora.
    @Slot(str)
    def _on_broker_connected(self, broker_key: str):
        """
        Slot para atualizar o status de conexão das corretoras quando o sinal `broker_connected` é emitido.
        Reordena as abas para refletir as mudanças de conexão.
        """
        connected_brokers = self.broker_manager.get_connected_brokers()
        for key in self.broker_manager.get_brokers():  # Itera sobre todas as corretoras para atualizar o status de conexão.
            self.broker_connected[key] = key in connected_brokers

        # [FIX 1] Chama _populate_broker_tabs sem argumento, a seleção será tratada no _on_broker_status_updated.
        self._populate_broker_tabs()

        logger.info(f"Bloco 4 - Status de conexão atualizado: {self.broker_connected}")

    # [FIX 1] Novo slot para armazenar a chave da corretora da aba selecionada.
    @Slot(int)
    def _on_tab_changed(self, index: int):
        """
        Slot chamado quando a aba selecionada no QTabWidget muda.
        Armazena a chave da corretora da aba recém-selecionada.
        """
        if index >= 0:
            # Obtém o texto da aba (que contém a chave da corretora).
            tab_text = self.broker_tabs.tabText(index)
            # Extrai a chave da corretora do texto da aba (ex: "ONEQUITY-915051 (H)").
            self.current_selected_broker_key = tab_text.split(' ')[0]
            logger.debug(f"Bloco 4 - Aba selecionada mudou para: {self.current_selected_broker_key}")
        else:
            self.current_selected_broker_key = None
            logger.debug("Bloco 4 - Nenhuma aba selecionada.")

    # [FIX 4] Novo slot para lidar com eventos de trade recebidos via stream.
    @Slot(dict)
    def _on_trade_event_received(self, trade_event_data: dict):
        """
        Slot para processar eventos de trade recebidos via stream do EA.
        Se a operação for bem-sucedida, solicita a atualização das posições para a corretora afetada.
        """
        broker_key = trade_event_data.get("broker_key")
        # Acessa o retcode do dicionário 'result' que agora está corretamente populado.
        result_retcode = trade_event_data.get("result", {}).get("retcode")

        # Códigos de retorno de sucesso do MQL5 (0 para sucesso, 10009 para TRADE_RETCODE_DONE)
        # TRADE_RETCODE_DONE = 10009
        # TRADE_RETCODE_REJECT = 10004
        # TRADE_RETCODE_INVALID = 10006
        # TRADE_RETCODE_INVALID_PRICE = 10014

        # Verifica se o retcode indica sucesso ou um estado que requer atualização.
        # O EA envia TRADE_EVENT para TRADE_RETCODE_DONE, REJECT, INVALID, INVALID_PRICE.
        # Vamos atualizar a GUI para DONE, e logar os outros.
        if result_retcode == 0 or result_retcode == 10009:  # 0 é o código de sucesso padrão, 10009 é TRADE_RETCODE_DONE
            logger.info(
                f"Bloco 4 - Evento de trade bem-sucedido recebido para {broker_key}. Solicitando atualização de posições.")
            self._request_positions_for_broker(broker_key)
            self.update_log(f"Evento de trade: Operação bem-sucedida para {broker_key}. Atualizando posições.")
        else:
            logger.warning(
                f"Bloco 4 - Evento de trade com erro/aviso recebido para {broker_key} (Retcode: {result_retcode}). Não solicitando atualização automática.")
            self.update_log(f"Evento de trade: Erro/Aviso para {broker_key} (Retcode: {result_retcode}).")

    # Bloco 5 - Gerenciamento de Abas e Sub-abas de Corretoras
    # Objetivo: Popular e gerenciar as abas das corretoras, incluindo a criação de sub-abas para
    # ordens abertas, posições pendentes e histórico de trades.
    # [FIX 1, 2] Remove o argumento select_key_candidate.
    def _populate_broker_tabs(self):
        """
        Popula e atualiza as abas das corretoras na interface, ordenando-as alfabeticamente.
        Cada aba de corretora contém sub-abas para ordens abertas, posições pendentes e histórico de trades.
        Esta função agora adiciona, remove e atualiza abas de forma inteligente, sem limpar tudo.
        A seleção da aba é baseada na aba previamente selecionada ou na primeira disponível.
        """
        logger.debug("Bloco 5 - Populando/Atualizando abas de corretoras.")

        all_brokers = self.broker_manager.get_brokers()
        # Chaves das corretoras que DEVEM ter uma aba (conectadas E registradas).
        active_broker_keys = {
            key for key in all_brokers.keys()
            if self.broker_connected.get(key, False) and self.broker_status.get(key, False)
        }

        # Armazena a chave da corretora atualmente selecionada antes de qualquer modificação.
        # [FIX 1] Tenta manter a seleção atual.
        key_to_select_after_update = None
        if self.broker_tabs.currentIndex() >= 0:
            current_tab_text = self.broker_tabs.tabText(self.broker_tabs.currentIndex())
            key_to_select_after_update = current_tab_text.split(' ')[0]  # Extrai a chave da corretora.

        # Mapeia chaves de corretoras para seus índices de aba atuais.
        current_tab_map = {}
        for i in range(self.broker_tabs.count()):
            tab_text = self.broker_tabs.tabText(i)
            broker_key = tab_text.split(' ')[0]
            current_tab_map[broker_key] = i

        # 1. Remover abas obsoletas (corretoras que não estão mais ativas).
        removed_count = 0
        for broker_key in list(current_tab_map.keys()):  # Itera sobre uma cópia para permitir remoção.
            if broker_key not in active_broker_keys:
                index_to_remove = current_tab_map[broker_key]
                self.broker_tabs.removeTab(index_to_remove)
                logger.info(f"Bloco 5 - Aba removida para corretora: {broker_key}")
                removed_count += 1
                # Atualiza o mapa após a remoção, pois os índices podem ter mudado.
                current_tab_map = {}
                for i in range(self.broker_tabs.count()):
                    tab_text = self.broker_tabs.tabText(i)
                    bk = tab_text.split(' ')[0]
                    current_tab_map[bk] = i

        # 2. Adicionar ou atualizar abas para corretoras ativas, mantendo a ordem alfabética.
        added_or_updated_count = 0
        sorted_active_broker_keys = sorted(list(active_broker_keys))

        # Para cada corretora que deveria ter uma aba...
        for broker_key in sorted_active_broker_keys:
            mode = self.broker_modes.get(broker_key, "Hedge")
            expected_tab_label = f"{broker_key} ({'H' if mode == 'Hedge' else 'N'})"

            if broker_key in current_tab_map:
                # A aba já existe, apenas atualiza o texto se necessário.
                current_index = current_tab_map[broker_key]
                if self.broker_tabs.tabText(current_index) != expected_tab_label:
                    self.broker_tabs.setTabText(current_index, expected_tab_label)
                    logger.debug(f"Bloco 5 - Texto da aba atualizado para {broker_key}.")
                added_or_updated_count += 1
            else:
                # A aba não existe, precisa ser adicionada na posição correta.
                tab = QWidget()
                tab_layout = QVBoxLayout(tab)
                sub_tabs = QTabWidget()

                open_orders_tab = self._create_open_orders_tab(broker_key)
                sub_tabs.addTab(open_orders_tab, "Ordens Abertas")

                pending_orders_tab = self._create_pending_orders_tab(broker_key)
                sub_tabs.addTab(pending_orders_tab, "Posições Pendentes")

                history_tab = self._create_history_tab(broker_key)
                sub_tabs.addTab(history_tab, "Histórico de Trades")

                tab_layout.addWidget(sub_tabs)
                tab.setLayout(tab_layout)

                # Encontra a posição correta para inserir a nova aba para manter a ordem alfabética.
                insert_index = 0
                for i in range(self.broker_tabs.count()):
                    existing_broker_key = self.broker_tabs.tabText(i).split(' ')[0]
                    if broker_key < existing_broker_key:
                        insert_index = i
                        break
                    insert_index = i + 1  # Se for maior que todos, adiciona no final.

                self.broker_tabs.insertTab(insert_index, tab, expected_tab_label)
                logger.info(f"Bloco 5 - Aba adicionada para corretora {broker_key} na posição {insert_index}.")
                added_or_updated_count += 1

                # Após adicionar, atualiza o mapa de índices.
                current_tab_map = {}
                for i in range(self.broker_tabs.count()):
                    tab_text = self.broker_tabs.tabText(i)
                    bk = tab_text.split(' ')[0]
                    current_tab_map[bk] = i

        # 3. Selecionar a aba desejada.
        # [FIX 1] Tenta selecionar a aba que estava ativa.
        if key_to_select_after_update and key_to_select_after_update in active_broker_keys:
            # Encontra o índice da aba correspondente à chave.
            index_to_set = -1
            for i in range(self.broker_tabs.count()):
                tab_text = self.broker_tabs.tabText(i)
                if tab_text.startswith(key_to_select_after_update):
                    index_to_set = i
                    break
            if index_to_set != -1:
                self.broker_tabs.setCurrentIndex(index_to_set)
                logger.info(f"Bloco 5 - Aba para {key_to_select_after_update} selecionada (mantendo seleção).")
            else:
                logger.warning(
                    f"Bloco 5 - Não foi possível selecionar a aba para {key_to_select_after_update} (não encontrada após atualização).")
        elif self.broker_tabs.count() > 0:
            # Se nenhuma chave específica para selecionar, e há abas, seleciona a primeira.
            self.broker_tabs.setCurrentIndex(0)
            logger.debug("Bloco 5 - Selecionada a primeira aba disponível (fallback).")
        else:
            # Se não houver abas, garante que nenhuma esteja selecionada.
            self.broker_tabs.setCurrentIndex(-1)
            logger.debug("Bloco 5 - Nenhuma aba disponível para seleção.")

        logger.info(f"Bloco 5 - Abas de corretoras atualizadas: {self.broker_tabs.count()} corretoras listadas. "
                    f"Removidas: {removed_count}, Adicionadas/Atualizadas: {added_or_updated_count}.")

    def _create_open_orders_tab(self, broker_key: str) -> QWidget:
        """
        Cria a sub-aba de ordens abertas com uma tabela para exibir as posições ativas.
        Configura as colunas da tabela e o estilo visual.
        """
        logger.debug(f"Bloco 5 - Criando sub-aba de ordens abertas para {broker_key}.")
        tab = QWidget()
        layout = QVBoxLayout(tab)
        table = QTableWidget()
        table.setColumnCount(11)
        table.setHorizontalHeaderLabels(
            ["Ticket", "Símbolo", "Tipo", "Volume", "Preço Entrada", "SL", "TP", "Lucro/Prejuízo", "Fechar",
             "Modificar", "Parcial"])

        # Define larguras de coluna para melhor visualização.
        table.setColumnWidth(0, 80)
        table.setColumnWidth(1, 100)
        table.setColumnWidth(2, 80)
        table.setColumnWidth(3, 80)
        table.setColumnWidth(4, 100)
        table.setColumnWidth(5, 80)
        table.setColumnWidth(6, 80)
        table.setColumnWidth(7, 120)
        table.setColumnWidth(8, 70)
        table.setColumnWidth(9, 70)
        table.setColumnWidth(10, 70)

        table.setMinimumHeight(400)
        table.setRowHeight(0, 30)  # Define uma altura padrão para as linhas.
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)  # Impede edição direta na tabela.
        table.setSelectionMode(QAbstractItemView.NoSelection)  # Desabilita seleção de itens.
        table.setAlternatingRowColors(True)  # Habilita cores alternadas para as linhas.
        table.setStyleSheet("""
            QTableWidget {
                alternate-background-color: #f0f0f0;
            }
        """)
        table.setObjectName(f"open_orders_{broker_key}")  # Define um nome de objeto para fácil identificação.
        layout.addWidget(table)
        tab.setLayout(layout)
        logger.debug(f"Bloco 5 - Sub-aba de ordens abertas criada para {broker_key}.")
        return tab

    def _create_pending_orders_tab(self, broker_key: str) -> QWidget:
        """
        Cria a sub-aba de posições pendentes com uma tabela para exibir as ordens pendentes.
        Configura as colunas da tabela e o estilo visual.
        """
        logger.debug(f"Bloco 5 - Criando sub-aba de posições pendentes para {broker_key}.")
        tab = QWidget()
        layout = QVBoxLayout(tab)
        table = QTableWidget()
        table.setColumnCount(9)
        table.setHorizontalHeaderLabels(
            ["Ticket", "Símbolo", "Tipo", "Volume", "Preço", "SL", "TP", "Fechar", "Modificar"])

        # Define larguras de coluna para melhor visualização.
        table.setColumnWidth(0, 80)
        table.setColumnWidth(1, 100)
        table.setColumnWidth(2, 80)
        table.setColumnWidth(3, 80)
        table.setColumnWidth(4, 100)
        table.setColumnWidth(5, 80)
        table.setColumnWidth(6, 80)
        table.setColumnWidth(7, 70)
        table.setColumnWidth(8, 70)

        table.setMinimumHeight(400)
        table.setRowHeight(0, 30)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.setStyleSheet("""
            QTableWidget {
                alternate-background-color: #f0f0f0;
            }
        """)
        table.setObjectName(f"pending_orders_{broker_key}")
        layout.addWidget(table)
        tab.setLayout(layout)
        logger.debug(f"Bloco 5 - Sub-aba de posições pendentes criada para {broker_key}.")
        return tab

    def _create_history_tab(self, broker_key: str) -> QWidget:
        """
        Cria a sub-aba de histórico de trades com uma tabela para exibir os trades passados.
        Configura as colunas da tabela e o estilo visual.
        """
        logger.debug(f"Bloco 5 - Criando sub-aba de histórico de trades para {broker_key}.")
        tab = QWidget()
        layout = QVBoxLayout(tab)
        table = QTableWidget()
        table.setColumnCount(8)
        table.setHorizontalHeaderLabels(
            ["Ticket", "Símbolo", "Tipo", "Volume", "Preço", "Lucro", "Tempo", "Comentário"])

        # Define larguras de coluna para melhor visualização.
        table.setColumnWidth(0, 80)
        table.setColumnWidth(1, 100)
        table.setColumnWidth(2, 80)
        table.setColumnWidth(3, 80)
        table.setColumnWidth(4, 100)
        table.setColumnWidth(5, 80)
        table.setColumnWidth(6, 120)
        table.setColumnWidth(7, 150)

        table.setMinimumHeight(400)
        table.setRowHeight(0, 30)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.setStyleSheet("""
            QTableWidget {
                alternate-background-color: #f0f0f0;
            }
        """)
        table.setObjectName(f"history_{broker_key}")
        layout.addWidget(table)
        tab.setLayout(layout)
        logger.debug(f"Bloco 5 - Sub-aba de histórico de trades criada para {broker_key}.")
        return tab

    # Bloco 6 - Solicitação e Atualização de Dados (Posições e Histórico)
    # Objetivo: Gerenciar a solicitação de posições e histórico de trades do EA,
    # e atualizar as tabelas correspondentes na interface.
    def _request_positions_for_registered_brokers(self):
        """
        Solicita posições para todas as corretoras que estão registradas e conectadas
        ao iniciar a BoletaTraderGui.
        """
        if hasattr(self.main_window, 'broker_status'):
            self.broker_status.update(self.main_window.broker_status)
            connected_brokers = self.broker_manager.get_connected_brokers()
            for broker_key in connected_brokers:
                if broker_key in self.broker_status and self.broker_status[broker_key]:
                    self._request_positions_for_broker(broker_key)
                    self.positions_requested[broker_key] = True
                    logger.info(
                        f"Bloco 6 - Posições solicitadas para corretora registrada {broker_key} ao iniciar BoletaTraderGui.")
                    self.update_log(f"Solicitando posições para {broker_key}...")
                else:
                    logger.info(f"Bloco 6 - Aguardando registro de {broker_key} ao iniciar BoletaTraderGui.")
        else:
            logger.warning("Bloco 6 - main_window não possui broker_status ao iniciar BoletaTraderGui.")

    @Slot()
    def _request_positions(self):
        """
        Solicita posições para todas as corretoras conectadas e registradas.
        Este método é tipicamente chamado pelo botão "Atualizar Agora".
        """
        # Reseta o status de posições solicitadas para todas as corretoras.
        for broker_key in self.broker_manager.get_connected_brokers():
            self.positions_requested[broker_key] = False

        connected_brokers = self.broker_manager.get_connected_brokers()
        for broker_key in connected_brokers:
            if broker_key in self.broker_status and self.broker_status[broker_key]:
                self._request_positions_for_broker(broker_key)
                self.positions_requested[broker_key] = True
                self.update_log(f"Solicitando posições para {broker_key}...")
            else:
                self.update_log(f"Aguardando registro de {broker_key} antes de solicitar posições.")
                logger.info(f"Bloco 6 - Aguardando registro de {broker_key} antes de solicitar posições.")

    def _request_positions_for_broker(self, broker_key):
        """
        Envia o comando ZMQ para solicitar as posições de uma corretora específica.
        """
        command = "POSITIONS"
        payload = {}
        request_id = f"positions_{broker_key}_{int(time.time())}"
        self._send_async_command(broker_key, command, payload, request_id)
        logger.info(
            f"Bloco 6 - Comando agendado: Solicitar posições ({command}) para {broker_key} com request_id {request_id}")

    @Slot(dict)
    def _update_positions(self, positions_data):
        """
        Atualiza as tabelas de ordens abertas e posições pendentes com os dados recebidos do EA.
        Separa as posições em abertas e pendentes e popula as tabelas correspondentes.
        """
        try:
            broker_key = positions_data.get("broker_key", "")
            # Tenta obter os dados da chave "data" ou da chave vazia (fallback).
            positions = positions_data.get("data", positions_data.get("", []))
            logger.info(f"Bloco 6 - Atualizando posições na GUI para {broker_key}, {len(positions)} ordens recebidas.")

            # Separa posições abertas e pendentes com base no tipo.
            open_positions = [pos for pos in positions if "PENDING" not in pos.get("type", "").upper()]
            pending_positions = [pos for pos in positions if "PENDING" in pos.get("type", "").upper()]

            # Itera sobre as abas para encontrar a corretora correta e atualizar suas tabelas.
            for i in range(self.broker_tabs.count()):
                if self.broker_tabs.tabText(i).startswith(broker_key):
                    tab = self.broker_tabs.widget(i)
                    # Acessa o QTabWidget das sub-abas dentro da aba da corretora.
                    sub_tabs = tab.layout().itemAt(0).widget()

                    # Atualiza tabela de ordens abertas.
                    open_table = sub_tabs.findChild(QTableWidget, f"open_orders_{broker_key}")
                    if open_table:
                        open_table.clearContents()
                        open_table.setRowCount(len(open_positions))
                        for row, pos in enumerate(open_positions):
                            self._populate_position_row(open_table, row, pos, broker_key)
                        logger.debug(
                            f"Bloco 6 - Tabela de ordens abertas atualizada para {broker_key} com {len(open_positions)} linhas.")
                    else:
                        logger.warning(f"Bloco 6 - Tabela de ordens abertas não encontrada para {broker_key}.")

                    # Atualiza tabela de posições pendentes.
                    pending_table = sub_tabs.findChild(QTableWidget, f"pending_orders_{broker_key}")
                    if pending_table:
                        pending_table.clearContents()
                        pending_table.setRowCount(len(pending_positions))
                        for row, pos in enumerate(pending_positions):
                            self._populate_pending_row(pending_table, row, pos, broker_key)
                        logger.debug(
                            f"Bloco 6 - Tabela de posições pendentes atualizada para {broker_key} com {len(pending_positions)} linhas.")
                    else:
                        logger.warning(f"Bloco 6 - Tabela de posições pendentes não encontrada para {broker_key}.")
                    break  # Sai do loop após encontrar e atualizar a aba da corretora.
            else:
                logger.warning(f"Bloco 6 - Aba não encontrada para corretora {broker_key}.")
                self.update_log(f"Erro: Aba não encontrada para corretora {broker_key}.")

            self.update_log(
                f"Lista de posições atualizada para {broker_key}: {len(open_positions)} abertas, {len(pending_positions)} pendentes.")
        except Exception as e:
            logger.error(f"Bloco 6 - Erro ao atualizar posições na GUI: {str(e)}")
            self.update_log(f"Erro ao atualizar posições na GUI: {str(e)}")

    @Slot(dict)
    def _update_history_trades(self, history_data):
        """
        Atualiza a tabela de histórico de trades com os dados recebidos do EA.
        """
        try:
            broker_key = history_data.get("broker_key", "")
            # Tenta obter os dados da chave "data" ou da chave vazia (fallback).
            trades = history_data.get("data", history_data.get("", []))
            logger.info(f"Bloco 6 - Atualizando histórico de trades para {broker_key}, {len(trades)} trades recebidos.")

            # Itera sobre as abas para encontrar a corretora correta e atualizar sua tabela de histórico.
            for i in range(self.broker_tabs.count()):
                if self.broker_tabs.tabText(i).startswith(broker_key):
                    tab = self.broker_tabs.widget(i)
                    # Acessa o QTabWidget das sub-abas dentro da aba da corretora.
                    sub_tabs = tab.layout().itemAt(0).widget()

                    history_table = sub_tabs.findChild(QTableWidget, f"history_{broker_key}")
                    if history_table:
                        history_table.clearContents()
                        history_table.setRowCount(len(trades))
                        for row, trade in enumerate(trades):
                            # Popula cada célula da linha com os dados do trade.
                            ticket_item = QTableWidgetItem(str(trade.get("ticket", "")))
                            ticket_item.setTextAlignment(Qt.AlignCenter)
                            history_table.setItem(row, 0, ticket_item)

                            symbol_item = QTableWidgetItem(str(trade.get("symbol", "")))
                            symbol_item.setTextAlignment(Qt.AlignCenter)
                            history_table.setItem(row, 1, symbol_item)

                            type_item = QTableWidgetItem(str(trade.get("type", "")))
                            type_item.setTextAlignment(Qt.AlignCenter)
                            history_table.setItem(row, 2, type_item)

                            volume_item = QTableWidgetItem(f"{float(trade.get('volume', 0.0)):.2f}")
                            volume_item.setTextAlignment(Qt.AlignCenter)
                            history_table.setItem(row, 3, volume_item)

                            price_item = QTableWidgetItem(f"{float(trade.get('price', 0.0)):.2f}")
                            price_item.setTextAlignment(Qt.AlignCenter)
                            history_table.setItem(row, 4, price_item)

                            profit_item = QTableWidgetItem(f"{float(trade.get('profit', 0.0)):.2f}")
                            profit_item.setTextAlignment(Qt.AlignCenter)
                            history_table.setItem(row, 5, profit_item)

                            time_item = QTableWidgetItem(self._format_timestamp(trade.get("time", 0)))
                            time_item.setTextAlignment(Qt.AlignCenter)
                            history_table.setItem(row, 6, time_item)

                            comment_item = QTableWidgetItem(str(trade.get("comment", "")))
                            comment_item.setTextAlignment(Qt.AlignCenter)
                            history_table.setItem(row, 7, comment_item)
                        logger.debug(
                            f"Bloco 6 - Tabela de histórico atualizada para {broker_key} com {len(trades)} linhas.")
                    else:
                        logger.warning(f"Bloco 6 - Tabela de histórico não encontrada para {broker_key}.")
                    break  # Sai do loop após encontrar e atualizar a aba da corretora.
            else:
                logger.warning(f"Bloco 6 - Aba não encontrada para corretora {broker_key}.")
                self.update_log(f"Erro: Aba não encontrada para corretora {broker_key}.")

            self.update_log(f"Histórico de trades atualizado para {broker_key} com {len(trades)} trades.")
        except Exception as e:
            logger.error(f"Bloco 6 - Erro ao atualizar histórico de trades: {str(e)}")
            self.update_log(f"Erro ao atualizar histórico de trades: {str(e)}")

    # Bloco 7 - Preenchimento de Linhas da Tabela e Ações de Botões
    # Objetivo: Preencher as linhas das tabelas de ordens abertas e pendentes,
    # e configurar os botões de ação (Fechar, Modificar, Parcial) para cada linha.
    def _populate_position_row(self, table, row, pos, broker_key):
        """
        Preenche uma linha na tabela de ordens abertas com os dados da posição e adiciona botões de ação.
        """
        # Preenche as células da tabela com os dados da posição.
        ticket_item = QTableWidgetItem(str(pos.get("ticket", "")))
        ticket_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 0, ticket_item)

        symbol_item = QTableWidgetItem(str(pos.get("symbol", "")))
        symbol_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 1, symbol_item)

        type_item = QTableWidgetItem(str(pos.get("type", "")))
        type_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 2, type_item)

        volume_item = QTableWidgetItem(f"{float(pos.get('volume', 0.0)):.2f}")
        volume_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 3, volume_item)

        price_open_item = QTableWidgetItem(f"{float(pos.get('price_open', 0.0)):.2f}")
        price_open_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 4, price_open_item)

        sl_item = QTableWidgetItem(f"{float(pos.get('sl', 0.0)):.2f}")
        sl_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 5, sl_item)

        tp_item = QTableWidgetItem(f"{float(pos.get('tp', 0.0)):.2f}")
        tp_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 6, tp_item)

        profit_item = QTableWidgetItem(f"{float(pos.get('profit', 0.0)):.2f}")
        profit_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 7, profit_item)

        # Cria e configura o botão "Fechar".
        close_btn = QPushButton("✕")
        close_btn.setMinimumHeight(30)
        close_btn.setStyleSheet("color: red; padding: 0px; margin: 0px;")
        # Conecta o botão à função _close_order, passando a linha, chave da corretora e a tabela.
        close_btn.clicked.connect(lambda checked, r=row, bk=broker_key: self._close_order(r, bk, table))

        # Cria e configura o botão "Modificar".
        modify_btn = QPushButton("⚠")
        modify_btn.setMinimumHeight(30)
        modify_btn.setStyleSheet("padding: 0px; margin: 0px;")
        # Conecta o botão à função _modify_order.
        modify_btn.clicked.connect(lambda checked, r=row, bk=broker_key: self._modify_order(r, bk, table))

        # Cria e configura o botão "Parcial".
        partial_btn = QPushButton("½")
        partial_btn.setMinimumHeight(30)
        partial_btn.setStyleSheet("padding: 0px; margin: 0px;")
        # Conecta o botão à função _partial_close.
        partial_btn.clicked.connect(lambda checked, r=row, bk=broker_key: self._partial_close(r, bk, table))

        # Habilita/desabilita os botões com base no status de registro da corretora.
        enabled = broker_key in self.broker_status and self.broker_status[broker_key]
        close_btn.setEnabled(enabled)
        modify_btn.setEnabled(enabled)
        partial_btn.setEnabled(enabled)

        # Adiciona os botões à tabela.
        table.setCellWidget(row, 8, close_btn)
        table.setCellWidget(row, 9, modify_btn)
        table.setCellWidget(row, 10, partial_btn)
        logger.debug(f"Bloco 7 - Botões de ação adicionados para ordem na linha {row} de {broker_key}.")

    def _populate_pending_row(self, table, row, pos, broker_key):
        """
        Preenche uma linha na tabela de posições pendentes com os dados da ordem e adiciona botões de ação.
        """
        # Preenche as células da tabela com os dados da ordem pendente.
        ticket_item = QTableWidgetItem(str(pos.get("ticket", "")))
        ticket_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 0, ticket_item)

        symbol_item = QTableWidgetItem(str(pos.get("symbol", "")))
        symbol_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 1, symbol_item)

        type_item = QTableWidgetItem(str(pos.get("type", "")))
        type_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 2, type_item)

        volume_item = QTableWidgetItem(f"{float(pos.get('volume', 0.0)):.2f}")
        volume_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 3, volume_item)

        price_item = QTableWidgetItem(f"{float(pos.get('price_open', 0.0)):.2f}")
        price_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 4, price_item)

        sl_item = QTableWidgetItem(f"{float(pos.get('sl', 0.0)):.2f}")
        sl_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 5, sl_item)

        tp_item = QTableWidgetItem(f"{float(pos.get('tp', 0.0)):.2f}")
        tp_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 6, tp_item)

        # Cria e configura o botão "Fechar".
        close_btn = QPushButton("✕")
        close_btn.setMinimumHeight(30)
        close_btn.setStyleSheet("color: red; padding: 0px; margin: 0px;")
        # Conecta o botão à função _close_order.
        close_btn.clicked.connect(lambda checked, r=row, bk=broker_key: self._close_order(r, bk, table))

        # Cria e configura o botão "Modificar".
        modify_btn = QPushButton("⚠")
        modify_btn.setMinimumHeight(30)
        modify_btn.setStyleSheet("padding: 0px; margin: 0px;")
        # Conecta o botão à função _modify_order.
        modify_btn.clicked.connect(lambda checked, r=row, bk=broker_key: self._modify_order(r, bk, table))

        # Habilita/desabilita os botões com base no status de registro da corretora.
        enabled = broker_key in self.broker_status and self.broker_status[broker_key]
        close_btn.setEnabled(enabled)
        modify_btn.setEnabled(enabled)

        # Adiciona os botões à tabela.
        table.setCellWidget(row, 7, close_btn)
        table.setCellWidget(row, 8, modify_btn)
        logger.debug(f"Bloco 7 - Botões de ação adicionados para ordem pendente na linha {row} de {broker_key}.")

    # Bloco 8 - Operações de Trading (Fechar, Modificar, Fechamento Parcial)
    # Objetivo: Implementar a lógica para fechar, modificar e realizar fechamento parcial de ordens/posições,
    # incluindo a interação com o EA via ZMQ e o tratamento de diferentes modos de operação (Hedge/Netting).
    def _close_order(self, row, broker_key, table):
        """
        Envia um comando para fechar uma ordem ou posição pendente.
        Determina o comando correto com base no nome do objeto da tabela (ordens abertas ou pendentes).
        """
        ticket = table.item(row, 0).text() if table.item(row, 0) else ""
        if ticket:
            # Escolhe o comando ZMQ apropriado com base na tabela.
            command = "TRADE_POSITION_CLOSE_ID" if table.objectName().startswith(
                "open_orders") else "TRADE_ORDER_CLOSE_ID"
            payload = {"ticket": int(ticket)}
            request_id = f"close_{broker_key}_{int(time.time())}"
            self.pending_tickets[request_id] = ticket  # Armazena o ticket para rastrear a resposta.

            self._send_async_command(broker_key, command, payload, request_id)
            self.update_log(
                f"Comando enviado: Fechar ordem #{ticket} para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
            logger.info(
                f"Bloco 8 - Comando agendado: Fechar ordem #{ticket} para {broker_key} com request_id {request_id}")
        else:
            self.update_log("Erro: Ticket da ordem não encontrado.")

    def _modify_order(self, row, broker_key, table):
        """
        Abre um diálogo para modificar uma ordem ou posição pendente.
        Permite ao usuário ajustar Stop Loss (SL), Take Profit (TP), volume e preço (para ordens pendentes).
        """
        ticket = table.item(row, 0).text() if table.item(row, 0) else ""
        symbol = table.item(row, 1).text() if table.item(row, 1) else ""
        order_type = table.item(row, 2).text() if table.item(row, 2) else ""

        if not ticket or not symbol:
            self.update_log("Erro: Ticket ou símbolo da ordem não encontrado.")
            return

        modify_dialog = QDialog(self)
        modify_dialog.setWindowTitle(f"Modificar Ordem #{ticket}")
        layout = QVBoxLayout(modify_dialog)

        # Campo para Stop Loss (SL).
        sl_input = QDoubleSpinBox()
        sl_input.setDecimals(2)
        sl_input.setMinimum(0.0)
        sl_input.setMaximum(999999.99)
        sl_input.setValue(float(table.item(row, 5).text() or 0.0))  # Preenche com o valor atual.
        layout.addWidget(QLabel("Stop Loss (SL):"))
        layout.addWidget(sl_input)

        # Campo para Take Profit (TP).
        tp_input = QDoubleSpinBox()
        tp_input.setDecimals(2)
        tp_input.setMinimum(0.0)
        tp_input.setMaximum(999999.99)
        tp_input.setValue(float(table.item(row, 6).text() or 0.0))  # Preenche com o valor atual.
        layout.addWidget(QLabel("Take Profit (TP):"))
        layout.addWidget(tp_input)

        # Campos adicionais para volume e preço, se for uma ordem pendente.
        vol_input = None
        price_input = None
        if table.objectName().startswith("pending_orders"):
            vol_input = QDoubleSpinBox()
            vol_input.setDecimals(2)
            vol_input.setMinimum(0.01)
            vol_input.setMaximum(9999.99)
            vol_input.setValue(float(table.item(row, 3).text() or 0.0))

            price_input = QDoubleSpinBox()
            price_input.setDecimals(5)
            price_input.setMinimum(0.0)
            price_input.setMaximum(999999.99)
            price_input.setValue(float(table.item(row, 4).text() or 0.0))

            layout.addWidget(QLabel("Volume:"))
            layout.addWidget(vol_input)
            layout.addWidget(QLabel("Preço:"))
            layout.addWidget(price_input)

        # Botão de confirmação para enviar o comando de modificação.
        confirm_btn = QPushButton("Confirmar")
        confirm_btn.clicked.connect(lambda: self._send_modify_command(
            broker_key, ticket, symbol, sl_input.value(), tp_input.value(),
            vol_input.value() if vol_input else None,
            price_input.value() if price_input else None,
            modify_dialog,
            order_type
        ))
        layout.addWidget(confirm_btn)
        modify_dialog.show()

    def _send_modify_command(self, broker_key, ticket, symbol, sl, tp, volume=None, price=None, dialog=None,
                             order_type=""):
        """
        Envia o comando ZMQ para modificar uma ordem ou posição.
        Ajusta o comando e o payload com base no modo de operação da corretora (Hedge/Netting)
        e se é uma ordem pendente ou posição aberta.
        """
        mode = self.broker_modes.get(broker_key, "Hedge")

        # Lógica para modo Hedge ou ordens pendentes.
        if mode == "Hedge" or "PENDING" in order_type.upper():
            command = "TRADE_POSITION_MODIFY" if "PENDING" not in order_type else "TRADE_ORDER_MODIFY"
            payload = {"ticket": int(ticket), "sl": sl, "tp": tp}
            if volume is not None:
                payload["volume"] = volume
            if price is not None:
                payload["price"] = price
        else:  # Lógica para modo Netting.
            command = "TRADE_POSITION_MODIFY"
            payload = {"ticket": int(ticket), "symbol": symbol, "sl": sl, "tp": tp}

        request_id = f"modify_{broker_key}_{int(time.time())}"
        self.pending_tickets[request_id] = ticket  # Armazena o ticket para rastrear a resposta.

        self._send_async_command(broker_key, command, payload, request_id)
        self.update_log(
            f"Comando enviado: Modificar ordem #{ticket} para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
        logger.info(
            f"Bloco 8 - Comando agendado: Modificar ordem #{ticket} para {broker_key} com request_id {request_id}")

        if dialog:
            dialog.close()

    def _partial_close(self, row, broker_key, table):
        """
        Abre um diálogo para realizar o fechamento parcial de uma ordem aberta.
        Permite ao usuário especificar o volume a ser fechado.
        """
        # Verifica se a operação é válida para a tabela atual (apenas ordens abertas).
        if not table.objectName().startswith("open_orders"):
            self.update_log("Fechamento parcial não disponível para ordens pendentes.")
            return

        ticket = table.item(row, 0).text() if table.item(row, 0) else ""
        position_type = table.item(row, 2).text() if table.item(row, 2) else ""
        symbol = table.item(row, 1).text() if table.item(row, 1) else ""
        current_volume = float(table.item(row, 3).text() or 0.0)

        if not ticket or not position_type or not symbol:
            self.update_log("Erro: Informações da ordem não encontradas.")
            return

        partial_dialog = QDialog(self)
        partial_dialog.setWindowTitle(f"Fechamento Parcial - Ordem #{ticket}")
        layout = QVBoxLayout(partial_dialog)

        # Campo para o volume a ser fechado parcialmente.
        volume_input = QDoubleSpinBox()
        volume_input.setDecimals(2)
        volume_input.setMinimum(0.01)
        volume_input.setMaximum(current_volume)  # O volume a fechar não pode ser maior que o volume atual.
        volume_input.setValue(current_volume / 2)  # Valor padrão: metade do volume atual.
        layout.addWidget(QLabel("Volume a Fechar:"))
        layout.addWidget(volume_input)

        # Botão de confirmação para enviar o comando de fechamento parcial.
        confirm_btn = QPushButton("Confirmar")
        confirm_btn.clicked.connect(lambda: self._send_partial_command(
            broker_key, ticket, position_type, symbol, volume_input.value(), partial_dialog
        ))
        layout.addWidget(confirm_btn)
        partial_dialog.show()

    def _send_partial_command(self, broker_key, ticket, position_type, symbol, volume, dialog=None):
        """
        Envia o comando ZMQ para realizar o fechamento parcial de uma ordem.
        Ajusta o comando com base no modo de operação da corretora (Hedge/Netting).
        """
        mode = self.broker_modes.get(broker_key, "Hedge")

        # Lógica para modo Hedge.
        if mode == "Hedge":
            command = "TRADE_POSITION_PARTIAL"
            payload = {"ticket": int(ticket), "volume": volume}
            request_id = f"partial_{broker_key}_{int(time.time())}"
            self.pending_tickets[request_id] = ticket  # Armazena o ticket para rastrear a resposta.

            self._send_async_command(broker_key, command, payload, request_id)
            self.update_log(
                f"Comando enviado: Fechamento parcial da ordem #{ticket} ({volume}) para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
            logger.info(
                f"Bloco 8 - Comando agendado: Fechamento parcial da ordem #{ticket} para {broker_key} com request_id {request_id}")
        else:  # Lógica para modo Netting (redução de posição via ordem oposta).
            opposite_type = "SELL" if position_type == "BUY" else "BUY"
            command = f"TRADE_ORDER_TYPE_{opposite_type}"
            payload = {"symbol": symbol, "type": opposite_type, "volume": volume}
            request_id = f"partial_netting_{broker_key}_{int(time.time())}"
            self.pending_tickets[request_id] = ticket  # Armazena o ticket para rastrear a resposta.

            self._send_async_command(broker_key, command, payload, request_id)
            self.update_log(
                f"Comando enviado: Redução de posição #{ticket} via ordem oposta ({opposite_type} {volume}) para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
            logger.info(
                f"Bloco 8 - Comando agendado: Redução de posição #{ticket} para {broker_key} com request_id {request_id}")

        if dialog:
            dialog.close()

    # Bloco 9 - Comunicação ZMQ e Tratamento de Respostas
    # Objetivo: Enviar comandos assíncronos ao EA via ZMQ e processar as respostas recebidas,
    # atualizando a interface e o log de atividades.
    def _send_async_command(self, broker_key, command, payload, request_id):
        """
        Envia um comando assíncrono para o EA via ZMQ.
        Cria uma tarefa asyncio para enviar o comando sem bloquear a interface.
        """
        try:
            asyncio.create_task(
                self.zmq_router.send_command_to_broker(broker_key, command, payload, request_id)
            )
            logger.info(f"Bloco 9 - Comando {command} enviado para {broker_key} com request_id: {request_id}")
        except Exception as e:
            logger.error(f"Bloco 9 - Erro ao enviar comando {command} para {broker_key}: {str(e)}")
            self.update_log(f"Erro ao enviar comando para {broker_key}: {str(e)}")

    @Slot(dict)
    def _update_trade_response(self, response):
        """
        Processa as respostas de operações de trade recebidas do EA.
        Atualiza o log de atividades e solicita uma nova lista de posições se a operação foi bem-sucedida.
        """
        status = response.get("status", "unknown")
        message = response.get("message", "")
        broker_key = response.get("broker_key", "")
        request_id = response.get("request_id", "")

        if status == "OK":
            # Identifica o tipo de operação com base no request_id.
            if any(key in request_id.lower() for key in ["close", "partial", "modify", "partial_netting"]):
                operation = "Fechada" if "close" in request_id.lower() else \
                    "Fechamento Parcial" if "partial" in request_id.lower() else \
                        "Modificada" if "modify" in request_id.lower() else "Reduzida"

                ticket = self.pending_tickets.get(request_id, "desconhecido")
                self.update_log(
                    f"{operation} ordem #{ticket} para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
                logger.info(
                    f"Bloco 9 - Solicitando atualização de posições para {broker_key} após operação bem-sucedida com request_id {request_id}.")
                self._request_positions_for_broker(broker_key)  # Solicita atualização das posições.
            else:
                self.update_log(f"Sucesso ({broker_key}): {message}")
                logger.debug(
                    f"Bloco 9 - Nenhuma atualização de posições solicitada para {broker_key}, request_id {request_id} não é operação de alteração.")
        else:
            error_message = response.get("error_message", "Erro desconhecido")
            self.update_log(f"Erro ({broker_key}): {error_message}")
            logger.error(f"Bloco 9 - Erro na resposta de {broker_key}: {error_message}")

        # Remove o ticket da lista de pendentes após o processamento da resposta.
        if request_id in self.pending_tickets:
            del self.pending_tickets[request_id]

    # Bloco 10 - Gerenciamento de Log e Funções Auxiliares
    # Objetivo: Gerenciar a exibição de mensagens no log da interface e fornecer funções auxiliares,
    # como a formatação de timestamps.
    @Slot(str)
    def update_log(self, message):
        """
        Atualiza a área de log da interface com uma nova mensagem.
        Filtra mensagens para exibir apenas as relevantes e limita o número de linhas para evitar sobrecarga.
        """
        # Filtra as mensagens para exibir apenas as que contêm certas palavras-chave.
        if any(key in message for key in
               ["Fechada", "Fechamento Parcial", "Modificada", "Reduzida", "Lista de posições atualizada",
                "Solicitando posições", "Erro"]):
            self.log_area.append(message)

            # Limita o número de linhas no log para 500.
            lines = self.log_area.toPlainText().split('\n')
            if len(lines) > 500:
                self.log_area.setText('\n'.join(lines[-500:]))
        logger.debug(f"Bloco 10 - Mensagem de log filtrada: {message}")

    def _format_timestamp(self, timestamp: int) -> str:
        """
        Formata um timestamp UNIX (inteiro) para uma string de data e hora legível.
        Retorna uma string vazia em caso de erro na formatação.
        """
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
        except Exception as e:
            logger.error(f"Bloco 10 - Erro ao formatar timestamp {timestamp}: {str(e)}")
            return ""


# Bloco 11 - Execução Direta (Apenas para Testes/Desenvolvimento)
# Objetivo: Permitir a execução direta deste arquivo para testes isolados da GUI.
# Este bloco não é executado quando o arquivo é importado como um módulo.
if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    # Instâncias mock para permitir a execução isolada.
    # Em um ambiente real, estas seriam instâncias válidas dos seus objetos.
    mock_config = None
    mock_broker_manager = None
    mock_zmq_router = None
    mock_zmq_message_handler = None
    mock_main_window = None

    window = BoletaTraderGui(mock_config, mock_broker_manager, mock_zmq_router, mock_zmq_message_handler,
                             mock_main_window)
    window.show()
    sys.exit(app.exec())

# Arquivo: gui/boleta_trader_gui.py
# Versão: 1.0.9.k - Envio 5