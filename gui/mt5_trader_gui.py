# gui/mt5_trader_gui.py
# Versão 1.0.9.g - Correção Final
# Ajustes:
# - Código reorganizado em 9 blocos modulares para melhor organização e manutenção.
# - Correção do erro no streaming (request_id inconsistente entre START_STREAM_OHLC_INDICATORS e STOP_STREAM_OHLC_INDICATORS).
# - Adicionado controle de estado para desabilitar/habilitar os botões START_STREAM_OHLC_INDICATORS e STOP_STREAM_OHLC_INDICATORS.
# - Mantidas todas as funcionalidades das versões anteriores (1.0.9.f e 1.0.9.g):
#   - Interface com abas Administrativo, Trading, Indicadores.
#   - Suporte a comandos single-shot (GET_INDICATOR_MA, GET_OHLC, GET_TICK).
#   - Suporte a streaming (START/STOP_STREAM_OHLC_INDICATORS).
#   - Copy Trade, validações, sinais e logs.
# - Alinhado com ZmqTraderBridge 1.11 e ZmqRouter 1.0.9.b.

# Bloco 1 - Importações e Configuração Inicial
# Objetivo: Importar bibliotecas necessárias e configurar o logging para depuração e monitoramento.
# Este bloco define as dependências do sistema e o formato de logs para rastrear eventos e erros.

import sys
import json
import time
import logging
import asyncio
from PySide6.QtWidgets import (
    QApplication, QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QPushButton, QTextEdit, QTabWidget, QFormLayout,
    QLineEdit, QCheckBox, QLabel, QGridLayout, QTableWidget, QTableWidgetItem, QHeaderView
)
from PySide6.QtCore import Slot

# Configuração de logging para registrar eventos e erros em detalhes.
# Formato inclui timestamp, nível de log (DEBUG, INFO, ERROR) e mensagem.
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Bloco 2 - Inicialização da Classe e Estrutura Geral
# Objetivo: Definir a classe MT5TraderGui, inicializar atributos e chamar métodos de setup.
# Este bloco serve como ponto de entrada para a interface gráfica, armazenando referências a configurações,
# gerenciadores de corretoras, roteadores ZMQ e a janela principal.
# Ajuste (versão 1.0.9.g - Suporte a múltiplas corretoras):
# - Alterado self.stream_ohlc_indicators_request_id para um dicionário que armazena request_id por corretora.
# - Adicionado dicionário para rastrear estado de streaming ativo por corretora.

class MT5TraderGui(QDialog):
    def __init__(self, config, broker_manager, zmq_router, zmq_message_handler, main_window, parent=None):
        """
        Inicializa a interface gráfica MT5TraderGui.

        Args:
            config: Configurações da aplicação.
            broker_manager: Gerenciador de corretoras conectadas.
            zmq_router: Objeto para roteamento de mensagens ZMQ ao EA.
            zmq_message_handler: Manipulador de mensagens recebidas via ZMQ.
            main_window: Referência à janela principal para atualização de status.
            parent: Widget pai (opcional, padrão None).
        """
        super().__init__(parent)
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.zmq_message_handler = zmq_message_handler
        self.main_window = main_window
        self.copy_trade_enabled = False
        # Dicionário para armazenar o request_id do último START_STREAM_OHLC_INDICATORS por corretora.
        # Usado para garantir que o STOP_STREAM_OHLC_INDICATORS reutilize o mesmo request_id da corretora correspondente.
        self.stream_ohlc_indicators_request_ids = {}
        # Dicionário para rastrear se o streaming está ativo para cada corretora.
        self.streaming_active_by_broker = {}
        self.setWindowTitle("MT5Trader GUI")
        self.setGeometry(100, 100, 800, 600)  # Aumentado para acomodar a tabela
        self.setMinimumWidth(800)
        self.setup_ui()
        self._connect_signals()
        self._populate_brokers()
        self._update_buttons()
        logger.info("MT5TraderGui inicializado.")

    # Bloco 3 - Configuração da Interface Gráfica (UI Setup)
    # Objetivo: Definir o layout da interface gráfica, widgets (botões, campos de texto, abas, tabelas) e
    # organizar a estrutura visual da GUI.
    # Este bloco cria as abas Administrativo, Trading e Indicadores, além de campos de entrada e botões para
    # interação com o usuário.

    def setup_ui(self):
        """
        Configura a interface gráfica da aplicação, incluindo layout, abas e widgets.
        Cria as seções para seleção de corretora, abas de funcionalidades (Administrativo, Trading, Indicadores)
        e área de log.
        """
        layout = QVBoxLayout(self)

        # Widget para seleção de corretora.
        self.broker_combo = QComboBox()
        layout.addWidget(QLabel("Selecione a Corretora:"))
        layout.addWidget(self.broker_combo)

        # Checkbox para ativar/desativar funcionalidade de Copy Trade.
        self.copy_trade_checkbox = QCheckBox("Ativar Copy Trade (replicar ordens para outra conta)")
        self.copy_trade_checkbox.stateChanged.connect(self.toggle_copy_trade)
        layout.addWidget(self.copy_trade_checkbox)

        # Botão para parar monitoramento (inicialmente desativado).
        self.monitor_btn = QPushButton("Parar Monitoramento")
        self.monitor_btn.setMaximumWidth(150)
        self.monitor_btn.setStyleSheet("padding: 5px;")
        self.monitor_btn.setEnabled(False)
        layout.addWidget(self.monitor_btn)

        # Widget de abas para organizar funcionalidades por categoria.
        tabs = QTabWidget()
        layout.addWidget(tabs)

        # Aba Administrativo: Comandos de informações gerais e histórico.
        admin_tab = QWidget()
        admin_layout = QVBoxLayout(admin_tab)
        admin_buttons_layout = QGridLayout()

        # Lista de comandos administrativos disponíveis.
        admin_commands = [
            "PING", "GET_STATUS_INFO", "GET_BROKER_INFO", "GET_BROKER_SERVER",
            "GET_BROKER_PATH", "GET_ACCOUNT_INFO", "GET_ACCOUNT_BALANCE",
            "GET_ACCOUNT_LEVERAGE", "GET_ACCOUNT_FLAGS", "GET_ACCOUNT_MARGIN",
            "GET_ACCOUNT_STATE", "GET_TIME_SERVER", "POSITIONS", "ORDERS"
        ]
        self.admin_buttons = {}
        for i, cmd in enumerate(admin_commands):
            btn = QPushButton(cmd)
            btn.setMaximumWidth(150)
            btn.setStyleSheet("padding: 5px;")
            btn.clicked.connect(lambda checked, c=cmd: self.send_admin_command(c))
            self.admin_buttons[cmd] = btn
            row = i // 3
            col = i % 3
            admin_buttons_layout.addWidget(btn, row, col)

        # Campos para configuração do comando HISTORY_DATA.
        history_data_layout = QFormLayout()
        self.history_data_symbol = QLineEdit("BTCUSD")
        self.history_data_symbol.setMaximumWidth(100)
        self.history_data_timeframe = QLineEdit("H1")
        self.history_data_timeframe.setMaximumWidth(100)
        self.history_data_start = QLineEdit(str(int(time.time()) - 86400))
        self.history_data_start.setMaximumWidth(100)
        self.history_data_end = QLineEdit(str(int(time.time())))
        self.history_data_end.setMaximumWidth(100)
        history_data_layout.addRow("Símbolo:", self.history_data_symbol)
        history_data_layout.addRow("Timeframe:", self.history_data_timeframe)
        history_data_layout.addRow("Start Time:", self.history_data_start)
        history_data_layout.addRow("End Time:", self.history_data_end)
        self.history_data_btn = QPushButton("HISTORY_DATA")
        self.history_data_btn.setMaximumWidth(150)
        self.history_data_btn.setStyleSheet("padding: 5px;")
        self.history_data_btn.clicked.connect(lambda: self.send_admin_command("HISTORY_DATA"))
        history_data_layout.addRow("", self.history_data_btn)

        # Campos para configuração do comando HISTORY_TRADES.
        history_trades_layout = QFormLayout()
        self.history_trades_start = QLineEdit(str(int(time.time()) - 86400))
        self.history_trades_start.setMaximumWidth(100)
        self.history_trades_end = QLineEdit(str(int(time.time())))
        self.history_trades_end.setMaximumWidth(100)
        history_trades_layout.addRow("Start Time:", self.history_trades_start)
        history_trades_layout.addRow("End Time:", self.history_trades_end)
        self.history_trades_btn = QPushButton("HISTORY_TRADES")
        self.history_trades_btn.setMaximumWidth(150)
        self.history_trades_btn.setStyleSheet("padding: 5px;")
        self.history_trades_btn.clicked.connect(lambda: self.send_admin_command("HISTORY_TRADES"))
        history_trades_layout.addRow("", self.history_trades_btn)

        admin_layout.addLayout(admin_buttons_layout)
        admin_layout.addLayout(history_data_layout)
        admin_layout.addLayout(history_trades_layout)
        tabs.addTab(admin_tab, "Administrativo")

        # Aba Trading: Comandos para operações de mercado (compra, venda, modificação de posições).
        trade_tab = QWidget()
        trade_layout = QVBoxLayout(trade_tab)
        trade_form_layout = QFormLayout()

        self.trade_symbol = QLineEdit("BTCUSD")
        self.trade_volume = QLineEdit("0.1")
        self.trade_price = QLineEdit("0.0")
        self.trade_sl = QLineEdit("0.0")
        self.trade_tp = QLineEdit("0.0")
        self.trade_deviation = QLineEdit("10")
        self.trade_comment = QLineEdit("Teste GUI")
        self.trade_symbol.setMaximumWidth(100)
        self.trade_volume.setMaximumWidth(100)
        self.trade_price.setMaximumWidth(100)
        self.trade_sl.setMaximumWidth(100)
        self.trade_tp.setMaximumWidth(100)
        self.trade_deviation.setMaximumWidth(100)
        self.trade_comment.setMaximumWidth(100)
        trade_form_layout.addRow("Símbolo:", self.trade_symbol)
        trade_form_layout.addRow("Volume:", self.trade_volume)
        trade_form_layout.addRow("Preço:", self.trade_price)
        trade_form_layout.addRow("SL:", self.trade_sl)
        trade_form_layout.addRow("TP:", self.trade_tp)
        trade_form_layout.addRow("Deviation:", self.trade_deviation)
        trade_form_layout.addRow("Comentário:", self.trade_comment)

        trade_buttons_layout = QGridLayout()
        trade_commands = [
            "ORDER_TYPE_BUY", "ORDER_TYPE_SELL",
            "ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_SELL_LIMIT",
            "ORDER_TYPE_BUY_STOP", "ORDER_TYPE_SELL_STOP"
        ]
        self.trade_buttons = {}
        for i, cmd in enumerate(trade_commands):
            btn = QPushButton(cmd)
            btn.setMaximumWidth(150)
            btn.setStyleSheet("padding: 5px;")
            btn.clicked.connect(lambda checked, c=f"TRADE_{cmd}": self.send_trade_command(c))
            self.trade_buttons[cmd] = btn
            row = i // 3
            col = i % 3
            trade_buttons_layout.addWidget(btn, row, col)

        modify_layout = QFormLayout()
        self.trade_ticket = QLineEdit("0")
        self.modify_sl = QLineEdit("0.0")
        self.modify_tp = QLineEdit("0.0")
        self.partial_volume = QLineEdit("0.1")
        self.close_symbol = QLineEdit("BTCUSD")
        self.trade_ticket.setMaximumWidth(100)
        self.modify_sl.setMaximumWidth(100)
        self.modify_tp.setMaximumWidth(100)
        self.partial_volume.setMaximumWidth(100)
        self.close_symbol.setMaximumWidth(100)
        modify_layout.addRow("Ticket (Modificar/Fechar):", self.trade_ticket)
        modify_layout.addRow("SL (Modificar):", self.modify_sl)
        modify_layout.addRow("TP (Modificar):", self.modify_tp)
        modify_layout.addRow("Volume Parcial:", self.partial_volume)
        modify_layout.addRow("Símbolo (Fechar):", self.close_symbol)

        modify_buttons_layout = QGridLayout()
        modify_commands = [
            "POSITION_MODIFY", "POSITION_PARTIAL",
            "POSITION_CLOSE_ID", "POSITION_CLOSE_SYMBOL",
            "ORDER_MODIFY", "ORDER_CANCEL"
        ]
        self.modify_buttons = {}
        for i, cmd in enumerate(modify_commands):
            btn = QPushButton(cmd)
            btn.setMaximumWidth(150)
            btn.setStyleSheet("padding: 5px;")
            btn.clicked.connect(lambda checked, c=f"TRADE_{cmd}": self.send_trade_command(c))
            self.modify_buttons[cmd] = btn
            row = i // 3
            col = i % 3
            modify_buttons_layout.addWidget(btn, row, col)

        trade_layout.addLayout(trade_form_layout)
        trade_layout.addLayout(trade_buttons_layout)
        trade_layout.addLayout(modify_layout)
        trade_layout.addLayout(modify_buttons_layout)
        tabs.addTab(trade_tab, "Trading")

        # Aba Indicadores: Comandos para indicadores e streaming de dados.
        indicators_tab = QWidget()
        indicators_layout = QVBoxLayout(indicators_tab)

        # Tabela para configurar múltiplos ativos e indicadores para streaming.
        indicators_layout.addWidget(QLabel("Configuração de Streaming OHLC + Indicadores:"))
        self.stream_table = QTableWidget()
        self.stream_table.setColumnCount(3)
        self.stream_table.setHorizontalHeaderLabels(["Símbolo", "Timeframe", "Indicadores (ex: MA,9;MA,21)"])
        self.stream_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.stream_table.setRowCount(1)  # Começa com uma linha vazia
        indicators_layout.addWidget(self.stream_table)

        # Botões para adicionar e remover linhas da tabela de streaming.
        table_row_buttons_layout = QHBoxLayout()
        add_row_btn = QPushButton("Adicionar Ativo")
        add_row_btn.clicked.connect(self.add_stream_row)
        remove_row_btn = QPushButton("Remover Ativo")
        remove_row_btn.clicked.connect(self.remove_stream_row)
        table_row_buttons_layout.addWidget(add_row_btn)
        table_row_buttons_layout.addWidget(remove_row_btn)
        indicators_layout.addLayout(table_row_buttons_layout)

        # Botões para iniciar/parar o streaming encapsulado de OHLC e indicadores.
        stream_control_buttons_layout = QHBoxLayout()
        self.start_stream_indicators_btn = QPushButton("START_STREAM_OHLC_INDICATORS")
        self.start_stream_indicators_btn.setMaximumWidth(250)
        self.start_stream_indicators_btn.setStyleSheet("padding: 5px;")
        self.start_stream_indicators_btn.clicked.connect(
            lambda: self.send_admin_command("START_STREAM_OHLC_INDICATORS"))
        stream_control_buttons_layout.addWidget(self.start_stream_indicators_btn)

        self.stop_stream_indicators_btn = QPushButton("STOP_STREAM_OHLC_INDICATORS")
        self.stop_stream_indicators_btn.setMaximumWidth(250)
        self.stop_stream_indicators_btn.setStyleSheet("padding: 5px;")
        self.stop_stream_indicators_btn.clicked.connect(
            lambda: self.send_admin_command("STOP_STREAM_OHLC_INDICATORS"))
        stream_control_buttons_layout.addWidget(self.stop_stream_indicators_btn)
        indicators_layout.addLayout(stream_control_buttons_layout)

        # Seção para comandos single-shot de indicadores (GET_INDICATOR_MA, GET_OHLC, GET_TICK).
        indicators_layout.addWidget(QLabel("\nComandos de Indicadores (Single-Shot):"))
        indicators_form_layout = QFormLayout()
        self.indicator_symbol = QLineEdit("EURUSD")
        self.indicator_timeframe = QLineEdit("H1")
        self.indicator_period = QLineEdit("20")
        self.indicator_symbol.setMaximumWidth(100)
        self.indicator_timeframe.setMaximumWidth(100)
        self.indicator_period.setMaximumWidth(100)
        indicators_form_layout.addRow("Símbolo:", self.indicator_symbol)
        indicators_form_layout.addRow("Timeframe:", self.indicator_timeframe)
        indicators_form_layout.addRow("Período (MA):", self.indicator_period)
        indicators_layout.addLayout(indicators_form_layout)

        # Botões para comandos single-shot de indicadores.
        single_shot_buttons_layout = QHBoxLayout()
        indicator_commands = ["GET_INDICATOR_MA", "GET_OHLC", "GET_TICK"]
        self.indicator_buttons = {}
        for cmd in indicator_commands:
            btn = QPushButton(cmd)
            btn.setMaximumWidth(150)
            btn.setStyleSheet("padding: 5px;")
            btn.clicked.connect(lambda checked, c=cmd: self.send_admin_command(c))
            self.indicator_buttons[cmd] = btn
            single_shot_buttons_layout.addWidget(btn)
        indicators_layout.addLayout(single_shot_buttons_layout)

        tabs.addTab(indicators_tab, "Indicadores")  # Adiciona a aba Indicadores ao final

        # Área de log para exibir mensagens, respostas e erros.
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(300)
        layout.addWidget(QLabel("Log de Comandos e Respostas:"))
        layout.addWidget(self.log_area)

        # Botão para fechar a interface.
        stop_btn = QPushButton("Fechar")
        stop_btn.setMaximumWidth(150)
        stop_btn.setStyleSheet("padding: 5px;")
        stop_btn.clicked.connect(self.close)
        layout.addWidget(stop_btn)

    # Bloco 4 - Conexões de Sinais e Atualizações de Interface
    # Objetivo: Definir conexões de sinais (eventos) e métodos para atualização da interface.
    # Este bloco lida com a interação entre eventos (sinais do Qt), atualização de widgets e exibição de
    # dados recebidos do EA (posições, ordens, indicadores, etc.) na área de log.
    # Ajuste (versão 1.0.9.g - Correção AttributeError):
    # - Alterado para usar self.streaming_active_by_broker em vez de self.streaming_active no método _update_buttons.
    # Ajuste (versão 1.0.9.k - Correção Streaming Multi-Corretoras):
    # - Adicionado _handle_broker_status_updated para gerenciar estado de streaming de todas as corretoras.
    # - Revisada lógica de _update_buttons para consistência em cenários de corretoras não selecionadas.

    def _connect_signals(self):
        """
        Conecta sinais do Qt para eventos de interface e sinais personalizados para
        atualização de dados recebidos do EA.
        """
        self.broker_combo.currentIndexChanged.connect(self._update_buttons)
        self.zmq_message_handler.log_message_received.connect(self.update_log)
        self.main_window.broker_status_updated.connect(self._handle_broker_status_updated)  # Novo slot
        self.main_window.broker_status_updated.connect(self._update_buttons)  # Mantido para compatibilidade
        self.main_window.broker_connected.connect(self._select_broker)
        self.zmq_message_handler.positions_received.connect(self._update_positions)
        self.zmq_message_handler.orders_received.connect(self._update_orders)
        self.zmq_message_handler.history_data_received.connect(self._update_history_data)
        self.zmq_message_handler.history_trades_received.connect(self._update_history_trades)
        self.zmq_message_handler.trade_response_received.connect(self._update_trade_response)
        self.zmq_message_handler.indicator_ma_received.connect(self._update_indicator_ma)
        self.zmq_message_handler.ohlc_received.connect(self._update_ohlc)
        self.zmq_message_handler.tick_received.connect(self._update_tick)
        self.zmq_message_handler.stream_ohlc_received.connect(self._update_stream_ohlc)
        self.zmq_message_handler.stream_ohlc_indicators_received.connect(
            self._update_stream_ohlc_indicators)
        logger.debug("Sinais conectados no MT5TraderGui.")

    def _populate_brokers(self):
        """Popula o combo box de corretoras com as corretoras conectadas disponíveis."""
        self.broker_combo.clear()
        connected_brokers = self.broker_manager.get_connected_brokers()
        for key in sorted(connected_brokers):
            self.broker_combo.addItem(key)
        self._update_buttons()
        logger.info(
            f"Lista de corretoras atualizada na QComboBox: {[self.broker_combo.itemText(i) for i in range(self.broker_combo.count())]}")

    @Slot(str)
    def _select_broker(self, broker_key: str):
        """
        Seleciona automaticamente uma corretora no combo box quando conectada.

        Args:
            broker_key: Chave da corretora a ser selecionada.
        """
        index = self.broker_combo.findText(broker_key)
        if index >= 0:
            self.broker_combo.setCurrentIndex(index)
            logger.info(f"Corretora {broker_key} selecionada automaticamente na QComboBox.")
        else:
            logger.debug(f"Corretora {broker_key} não encontrada na QComboBox.")

    @Slot(str)
    def _handle_broker_status_updated(self, broker_key: str):
        """
        Gerencia o estado de streaming de todas as corretoras quando o status de uma muda.
        Desativa o streaming de corretoras não registradas.

        Args:
            broker_key: Chave da corretora cujo status foi atualizado.
        """
        # Verifica todas as corretoras, não apenas a selecionada
        for key in self.streaming_active_by_broker.keys():
            is_registered = bool(
                key in self.main_window.broker_status and self.main_window.broker_status[key])
            if not is_registered and self.streaming_active_by_broker.get(key, False):
                self.streaming_active_by_broker[key] = False
                self.stream_ohlc_indicators_request_ids[key] = None
                logger.info(f"Streaming desativado para {key} devido a corretora não registrada.")
                self.update_log(f"Streaming desativado para {key} (corretora não registrada).")
        # Atualiza a interface apenas para a corretora selecionada
        if self.broker_combo.currentText() == broker_key:
            self._update_buttons()

    def _update_buttons(self):
        """Atualiza o estado (habilitado/desabilitado) dos botões com base na corretora selecionada e seu status."""
        selected_key = self.broker_combo.currentText()
        is_registered = bool(
            selected_key and selected_key in self.main_window.broker_status and self.main_window.broker_status[
                selected_key])

        # Verifica se o streaming está ativo para a corretora selecionada
        streaming_active = selected_key in self.streaming_active_by_broker and self.streaming_active_by_broker[
            selected_key]

        # Atualiza botões administrativos e de trading
        for btn in self.admin_buttons.values():
            btn.setEnabled(is_registered)
        self.history_data_btn.setEnabled(is_registered)
        self.history_trades_btn.setEnabled(is_registered)

        for btn in self.trade_buttons.values():
            btn.setEnabled(is_registered)
        for btn in self.modify_buttons.values():
            btn.setEnabled(is_registered)

        for btn in self.indicator_buttons.values():
            btn.setEnabled(is_registered)

        # Atualiza botões de streaming
        self.start_stream_indicators_btn.setEnabled(is_registered and not streaming_active)
        self.stop_stream_indicators_btn.setEnabled(is_registered and streaming_active)

        logger.debug(
            f"Botões atualizados para corretora {selected_key}. "
            f"Registrada: {is_registered}, Streaming ativo: {streaming_active}, "
            f"START habilitado: {self.start_stream_indicators_btn.isEnabled()}, "
            f"STOP habilitado: {self.stop_stream_indicators_btn.isEnabled()}"
        )

    def toggle_copy_trade(self, state):
        """
        Ativa ou desativa a funcionalidade de Copy Trade com base no estado do checkbox.

        Args:
            state: Estado do checkbox (2 para ativado, 0 para desativado).
        """
        self.copy_trade_enabled = state == 2
        log_msg = f"Copy Trade {'ativado' if self.copy_trade_enabled else 'desativado'}"
        logger.info(log_msg)
        self.update_log(log_msg)

    @Slot(str)
    def update_log(self, message):
        """
        Atualiza a área de log da interface com uma nova mensagem.
        Limita o número de linhas a 1000 para evitar sobrecarga.

        Args:
            message: Mensagem a ser exibida no log.
        """
        if "TICK" not in message:
            self.log_area.append(message)
            lines = self.log_area.toPlainText().split('\n')
            if len(lines) > 1000:
                self.log_area.setText('\n'.join(lines[-1000:]))

    @Slot(dict)
    def _update_positions(self, positions):
        """
        Atualiza a área de log com dados de posições recebidas do EA.

        Args:
            positions: Dicionário com dados de posições.
        """
        text = f"Posições: {json.dumps(positions, indent=2)}"
        self.log_area.append(text)
        logger.debug(f"Posições atualizadas: {text}")

    @Slot(dict)
    def _update_orders(self, orders):
        """
        Atualiza a área de log com dados de ordens recebidas do EA.

        Args:
            orders: Dicionário com dados de ordens.
        """
        text = f"Ordens: {json.dumps(orders, indent=2)}"
        self.log_area.append(text)
        logger.debug(f"Ordens atualizadas: {text}")

    @Slot(dict)
    def _update_history_data(self, history_data):
        """
        Atualiza a área de log com dados históricos de candles recebidos do EA.

        Args:
            history_data: Dicionário com dados históricos.
        """
        text = f"Histórico de Dados: {json.dumps(history_data, indent=2)}"
        self.log_area.append(text)
        logger.debug(f"Histórico de dados atualizado: {text}")

    @Slot(dict)
    def _update_history_trades(self, history_trades):
        """
        Atualiza a área de log com histórico de trades recebidos do EA.

        Args:
            history_trades: Dicionário com histórico de trades.
        """
        text = f"Histórico de Trades: {json.dumps(history_trades, indent=2)}"
        self.log_area.append(text)
        logger.debug(f"Histórico de trades atualizado: {text}")

    @Slot(dict)
    def _update_trade_response(self, trade_response):
        """
        Atualiza a área de log com resposta de um comando de trading.

        Args:
            trade_response: Dicionário com resposta do comando de trading.
        """
        status = "✅ Sucesso" if trade_response.get(
            'status') == 'OK' else f"❌ Erro: {trade_response.get('error_message', 'Desconhecido')}"
        text = (
            f"Resposta de Trading ({trade_response.get('broker_key')}): "
            f"{json.dumps(trade_response, indent=2)}\nStatus: {status}"
        )
        self.log_area.append(text)
        logger.debug(f"Resposta de trading atualizada: {text}")

    @Slot(dict)
    def _update_indicator_ma(self, data):
        """
        Atualiza a área de log com dados de média móvel recebidos.

        Args:
            data: Dicionário com dados de média móvel.
        """
        status = "✅ Sucesso" if 'ma_value' in data else "❌ Erro"
        text = f"Média Móvel ({data.get('broker_key')}): {json.dumps(data, indent=2)}\nStatus: {status}"
        self.log_area.append(text)
        logger.debug(f"Média Móvel atualizada: {text}")

    @Slot(dict)
    def _update_ohlc(self, data):
        """
        Atualiza a área de log com dados OHLC recebidos.

        Args:
            data: Dicionário com dados OHLC.
        """
        status = "✅ Sucesso" if 'ohlc' in data and data['ohlc'] else "❌ Erro"
        text = f"OHLC ({data.get('broker_key')}): {json.dumps(data, indent=2)}\nStatus: {status}"
        self.log_area.append(text)
        logger.debug(f"OHLC atualizado: {text}")

    @Slot(dict)
    def _update_tick(self, data):
        """
        Atualiza a área de log com dados de tick recebidos.

        Args:
            data: Dicionário com dados de tick.
        """
        status = "✅ Sucesso" if 'tick' in data and data['tick'] else "❌ Erro"
        text = f"Tick ({data.get('broker_key')}): {json.dumps(data, indent=2)}\nStatus: {status}"
        self.log_area.append(text)
        logger.debug(f"Tick atualizado: {text}")

    @Slot(dict)
    def _update_stream_ohlc(self, data):
        """
        Atualiza a área de log com dados de streaming OHLC recebidos.

        Args:
            data: Dicionário com dados de streaming OHLC.
        """
        status = "✅ Sucesso" if data.get("status") == "OK" or 'ohlc' in data else "❌ Erro"
        text = f"Stream OHLC ({data.get('broker_key')}): {json.dumps(data, indent=2)}\nStatus: {status}"
        self.log_area.append(text)
        logger.debug(f"Stream OHLC atualizado: {text}")

    @Slot(dict)
    def _update_stream_ohlc_indicators(self, data):
        """
        Atualiza a área de log com dados de streaming OHLC + Indicadores.
        Formata as informações de cada ativo (OHLC e indicadores) em linhas legíveis.

        Args:
            data: Dicionário com dados de streaming, contendo uma lista de entradas por ativo.
        """
        broker_key = data.get("broker_key", "N/A")
        entries = data.get("data", [])

        if not entries:
            self.log_area.append(f"Stream OHLC+Indicadores ({broker_key}): Nenhuma atualização de dados.")
            logger.debug(f"Stream OHLC+Indicadores ({broker_key}): Nenhuma atualização de dados.")
            return

        for entry in entries:
            symbol = entry.get("symbol", "N/A")
            timeframe = entry.get("timeframe", "N/A")
            ohlc = entry.get("ohlc", {})
            indicators = entry.get("indicators", [])

            ohlc_str = f"O:{ohlc.get('open', 0)} H:{ohlc.get('high', 0)} L:{ohlc.get('low', 0)} C:{ohlc.get('close', 0)}"

            indicators_str_list = []
            for ind in indicators:
                ind_type = ind.get('type', 'N/A')
                ind_period = ind.get('period', 'N/A')
                ind_value = ind.get('value', 'N/A')
                indicators_str_list.append(f"{ind_type}({ind_period}):{ind_value}")

            indicators_str = ", ".join(indicators_str_list)

            log_message = (
                f"Stream OHLC+Indicadores ({broker_key}) - {symbol} {timeframe}: "
                f"OHLC=[{ohlc_str}] | Indicadores=[{indicators_str}]"
            )
            self.log_area.append(log_message)
            logger.debug(f"Stream OHLC+Indicadores detalhe: {log_message}")

    # Bloco 5 - Gerenciamento de Tabela de Streaming
    # Objetivo: Definir métodos para gerenciar a tabela de configuração de streaming na aba Indicadores.
    # Este bloco permite adicionar e remover linhas na tabela usada para configurar múltiplos ativos e
    # indicadores para streaming.

    def add_stream_row(self):
        """
        Adiciona uma nova linha à tabela de configuração de streaming.
        Preenche a linha com valores padrão para facilitar a edição pelo usuário.
        """
        row = self.stream_table.rowCount()
        self.stream_table.insertRow(row)
        # Opcional: preencher com valores padrão para facilitar
        self.stream_table.setItem(row, 0, QTableWidgetItem("EURUSD"))
        self.stream_table.setItem(row, 1, QTableWidgetItem("M1"))
        self.stream_table.setItem(row, 2, QTableWidgetItem("MA,9;MA,21"))

    def remove_stream_row(self):
        """
        Remove a linha selecionada da tabela de configuração de streaming.
        Exibe uma mensagem de erro se nenhuma linha estiver selecionada.
        """
        row = self.stream_table.currentRow()
        if row >= 0:
            self.stream_table.removeRow(row)
        else:
            self.update_log("Selecione uma linha para remover.")

    # Bloco 6 - Comunicação com o EA (Funções Genéricas de Envio)
    # Objetivo: Definir métodos genéricos para envio de comandos ao EA via ZMQ.
    # Este bloco contém as funções base de comunicação usadas pelos métodos específicos de cada aba
    # (Administrativo, Trading, Indicadores). Lida com envio, tratamento de respostas e erros.

    async def send_command(self, broker_key, command, payload, command_type='admin', use_data_port=False):
        """
        Envia um comando genérico para o EA associado à corretora especificada.
        Usado para comandos administrativos e de trading.

        Args:
            broker_key: Chave da corretora para qual o comando será enviado.
            command: Nome do comando a ser executado (ex.: PING, TRADE_ORDER_TYPE_BUY).
            payload: Dicionário com os parâmetros do comando.
            command_type: Tipo de comando (admin ou trade, para fins de logging).
            use_data_port: Se True, usa a porta de dados para envio (usado para streaming e indicadores).
        """
        request_id = f"{command.lower()}_{broker_key}_{int(time.time())}"
        try:
            response = await self.zmq_router.send_command_to_broker(broker_key, command, payload, request_id, use_data_port=use_data_port)
            if isinstance(response, dict):
                if response.get("status") == "ERROR":
                    log_msg = f"Erro: {response.get('message', 'Falha desconhecida')}"
                    self.update_log(log_msg)
                    logger.error(f"Falha ao enviar {command} para {broker_key}: {response.get('message')}")
                else:
                    logger.info(f"Comando {command} enviado para {broker_key}: {response}")
            else:
                log_msg = f"Erro: Resposta inválida para {command}."
                self.update_log(log_msg)
                logger.error(f"Resposta inválida para {command} de {broker_key}: {response}")
        except asyncio.TimeoutError:
            log_msg = f"Erro: Timeout ao aguardar resposta para {command}."
            self.update_log(log_msg)
            logger.error(f"Timeout ao enviar {command} para {broker_key}")
        except Exception as e:
            log_msg = f"Erro ao enviar comando: {str(e)}"
            self.update_log(log_msg)
            logger.error(f"Exceção ao enviar {command} para {broker_key}: {str(e)}")

    async def send_data_command(self, broker_key, command, payload):
        """
        Envia um comando usando a porta de dados (DataPort) para o EA associado à corretora.
        Usado para comandos relacionados a indicadores e streaming.

        Args:
            broker_key: Chave da corretora para qual o comando será enviado.
            command: Nome do comando a ser executado (ex.: GET_INDICATOR_MA, START_STREAM_OHLC_INDICATORS).
            payload: Dicionário com os parâmetros do comando.
        """
        return await self.send_command(broker_key, command, payload, command_type='data', use_data_port=True)

    async def send_copy_trade(self, primary_broker, command, message):
        """
        Replica um comando de trading para outras corretoras conectadas, se Copy Trade estiver ativado.

        Args:
            primary_broker: Chave da corretora primária que originou o comando.
            command: Nome do comando de trading a ser replicado.
            message: Dicionário com dados do comando original (broker_key, payload, request_id).
        """
        connected_brokers = self.broker_manager.get_connected_brokers()
        for broker_key in connected_brokers:
            if broker_key != primary_broker and broker_key in self.main_window.broker_status and \
                    self.main_window.broker_status[broker_key]:
                copied_msg = message.copy()
                copied_msg["broker_key"] = broker_key
                copied_msg["request_id"] = f"{command.lower()}_{broker_key}_{int(time.time())}"
                await self.zmq_router.send_command_to_broker(broker_key, command, copied_msg["payload"],
                                                             copied_msg["request_id"])
                logger.info(f"Copy Trade: Enviado {command} para {broker_key}")

    # Bloco 7 - Comandos Administrativos (Aba Administrativo)
    # Objetivo: Definir a lógica para envio de comandos administrativos relacionados à aba Administrativo.
    # Este bloco lida com comandos para obtenção de informações gerais da corretora, conta, posições,
    # ordens e históricos (HISTORY_DATA, HISTORY_TRADES).
    # Ajuste (versão 1.0.9.g - Correção AttributeError):
    # - Alterado para usar self.streaming_active_by_broker em vez de self.streaming_active no método send_admin_command.

    def send_admin_command(self, command):
        """
        Envia um comando administrativo para o EA associado à corretora selecionada.
        Este método roteia para funções específicas dependendo do comando (Administrativo,
        Indicadores ou Streaming). Aqui, lida apenas com comandos da aba Administrativo.

        Args:
            command: Nome do comando a ser enviado (ex.: PING, HISTORY_DATA).
        """
        broker_key = self.broker_combo.currentText()
        if not broker_key:
            self.update_log("Erro: Nenhuma corretora selecionada.")
            logger.warning("Nenhuma corretora selecionada ao tentar enviar comando.")
            return
        payload = {}
        try:
            if command in ["PING", "GET_STATUS_INFO", "GET_BROKER_INFO", "GET_BROKER_SERVER",
                           "GET_BROKER_PATH", "GET_ACCOUNT_INFO", "GET_ACCOUNT_BALANCE",
                           "GET_ACCOUNT_LEVERAGE", "GET_ACCOUNT_FLAGS", "GET_ACCOUNT_MARGIN",
                           "GET_ACCOUNT_STATE", "GET_TIME_SERVER", "POSITIONS", "ORDERS"]:
                if command == "PING":
                    payload = {"timestamp": int(time.time())}
                asyncio.create_task(self.send_command(broker_key, command, payload, 'admin'))
            elif command == "HISTORY_DATA":
                symbol = self.history_data_symbol.text()
                if not symbol:
                    self.update_log("Erro: Símbolo vazio")
                    return
                timeframe = self.history_data_timeframe.text()
                if not timeframe:
                    self.update_log("Erro: Timeframe vazio")
                    return
                start_time = int(self.history_data_start.text())
                end_time = int(self.history_data_end.text())
                if start_time >= end_time:
                    self.update_log("Erro: Start Time deve ser menor que End Time")
                    return
                payload = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "start_time": start_time,
                    "end_time": end_time
                }
                asyncio.create_task(self.send_command(broker_key, command, payload, 'admin'))
            elif command == "HISTORY_TRADES":
                start_time = int(self.history_trades_start.text())
                end_time = int(self.history_trades_end.text())
                if start_time >= end_time:
                    self.update_log("Erro: Start Time deve ser menor que End Time")
                    return
                payload = {
                    "start_time": start_time,
                    "end_time": end_time
                }
                asyncio.create_task(self.send_command(broker_key, command, payload, 'admin'))
            elif command in ["START_STREAM_OHLC_INDICATORS", "STOP_STREAM_OHLC_INDICATORS"]:
                # Verifica se o streaming está ativo para a corretora selecionada.
                streaming_active = broker_key in self.streaming_active_by_broker and self.streaming_active_by_broker[
                    broker_key]
                if command == "START_STREAM_OHLC_INDICATORS" and streaming_active:
                    self.update_log("Erro: Streaming já ativo. Clique em STOP_STREAM_OHLC_INDICATORS primeiro.")
                    return
                self._send_indicators_command(command)
            elif command in ["GET_INDICATOR_MA", "GET_OHLC", "GET_TICK"]:
                self._send_indicators_command(command)
            else:
                self.update_log(f"Erro: Comando {command} não reconhecido.")
                logger.error(f"Comando {command} não reconhecido.")
        except ValueError as e:
            self.update_log(f"❌ Erro nos parâmetros: {str(e)}")
            logger.error(f"Erro nos parâmetros para comando {command}: {str(e)}")

    # Bloco 8 - Comandos de Trading (Aba Trading)
    # Objetivo: Definir a lógica para envio de comandos de trading relacionados à aba Trading.
    # Este bloco lida com operações de mercado, como compra, venda, modificação e fechamento de posições.
    # Ajuste (versão 1.0.9.g - Correção Comando):
    # - Alterado para enviar TRADE_POSITION_CLOSE em vez de TRADE_POSITION_CLOSE_SYMBOL, mantendo o nome do botão.

    def send_trade_command(self, command):
        """
        Envia um comando de trading para o EA associado à corretora selecionada.
        Lida com operações de mercado, incluindo ordens de compra/venda e gerenciamento de posições.

        Args:
            command: Nome do comando de trading a ser enviado (ex.: TRADE_ORDER_TYPE_BUY).
        """
        broker_key = self.broker_combo.currentText()
        if not broker_key:
            self.update_log("Erro: Nenhuma corretora selecionada.")
            logger.warning("Nenhuma corretora selecionada ao tentar enviar comando.")
            return
        payload = {}
        try:
            if command in [
                "TRADE_ORDER_TYPE_BUY", "TRADE_ORDER_TYPE_SELL",
                "TRADE_ORDER_TYPE_BUY_LIMIT", "TRADE_ORDER_TYPE_SELL_LIMIT",
                "TRADE_ORDER_TYPE_BUY_STOP", "TRADE_ORDER_TYPE_SELL_STOP"
            ]:
                symbol = self.trade_symbol.text()
                if not symbol:
                    self.update_log("Erro: Símbolo vazio")
                    return
                volume = float(self.trade_volume.text()) if self.trade_volume.text() else 0.0
                if volume <= 0:
                    self.update_log("Erro: Volume inválido")
                    return
                price = float(self.trade_price.text()) if self.trade_price.text() else 0.0
                sl = float(self.trade_sl.text()) if self.trade_sl.text() else 0.0
                tp = float(self.trade_tp.text()) if self.trade_tp.text() else 0.0
                deviation = int(self.trade_deviation.text()) if self.trade_deviation.text() else 10
                payload = {
                    "symbol": symbol,
                    "volume": volume,
                    "price": price,
                    "sl": sl,
                    "tp": tp,
                    "deviation": deviation,
                    "comment": self.trade_comment.text()
                }
            elif command == "TRADE_POSITION_MODIFY":
                ticket = int(self.trade_ticket.text())
                if ticket <= 0:
                    self.update_log("Erro: Ticket inválido")
                    return
                sl = float(self.modify_sl.text()) if self.modify_sl.text() else 0.0
                tp = float(self.modify_tp.text()) if self.modify_tp.text() else 0.0
                payload = {
                    "ticket": ticket,
                    "sl": sl,
                    "tp": tp
                }
            elif command == "TRADE_POSITION_PARTIAL":
                ticket = int(self.trade_ticket.text())
                if ticket <= 0:
                    self.update_log("Erro: Ticket inválido")
                    return
                volume = float(self.partial_volume.text()) if self.partial_volume.text() else 0.0
                if volume <= 0:
                    self.update_log("Erro: Volume parcial inválido")
                    return
                payload = {
                    "ticket": ticket,
                    "volume": volume
                }
            elif command in ["TRADE_POSITION_CLOSE_ID", "TRADE_ORDER_CANCEL"]:
                ticket = int(self.trade_ticket.text())
                if ticket <= 0:
                    self.update_log("Erro: Ticket inválido")
                    return
                payload = {
                    "ticket": ticket
                }
            elif command == "TRADE_POSITION_CLOSE_SYMBOL":
                symbol = self.close_symbol.text()
                if not symbol:
                    self.update_log("Erro: Símbolo vazio")
                    return
                payload = {
                    "symbol": symbol
                }
                # Ajusta o comando para o formato esperado pelo EA (TRADE_POSITION_CLOSE)
                command = "TRADE_POSITION_CLOSE"
            elif command == "TRADE_ORDER_MODIFY":
                ticket = int(self.trade_ticket.text())
                if ticket <= 0:
                    self.update_log("Erro: Ticket inválido")
                    return
                price = float(self.trade_price.text()) if self.trade_price.text() else 0.0
                sl = float(self.modify_sl.text()) if self.modify_sl.text() else 0.0
                tp = float(self.modify_tp.text()) if self.modify_tp.text() else 0.0
                payload = {
                    "ticket": ticket,
                    "price": price,
                    "sl": sl,
                    "tp": tp
                }
            asyncio.create_task(self.send_command(broker_key, command, payload, 'trade'))
            if self.copy_trade_enabled and command in [
                "TRADE_ORDER_TYPE_BUY", "TRADE_ORDER_TYPE_SELL",
                "TRADE_ORDER_TYPE_BUY_LIMIT", "TRADE_ORDER_TYPE_SELL_LIMIT",
                "TRADE_ORDER_TYPE_BUY_STOP", "TRADE_ORDER_TYPE_SELL_STOP",
                "TRADE_POSITION_CLOSE"
            ]:
                message = {"broker_key": broker_key, "command": command, "payload": payload,
                           "request_id": f"{command.lower()}_{broker_key}_{int(time.time())}"}
                asyncio.create_task(self.send_copy_trade(broker_key, command, message))
        except ValueError as e:
            self.update_log(f"❌ Erro nos parâmetros: {str(e)}")

    # Bloco 9 - Comandos de Indicadores e Streaming (Aba Indicadores)
    # Objetivo: Definir a lógica para envio de comandos relacionados à aba Indicadores, incluindo
    # comandos single-shot (GET_INDICATOR_MA, GET_OHLC, GET_TICK) e streaming (START/STOP_STREAM_OHLC_INDICATORS).
    # Este bloco corrige o problema de request_id inconsistente entre START e STOP de streaming,
    # armazenando e reutilizando o request_id do START, e adiciona controle de botões.
    # Ajuste (versão 1.0.9.g - Suporte a múltiplas corretoras):
    # - Alterado para usar dicionário de request_id e estado de streaming por corretora.
    # Ajuste (versão 1.0.9.k - Correção Streaming Multi-Corretoras):
    # - Adicionado tratamento de erro robusto para comandos de streaming.
    # - Garantida atualização de botões em todos os cenários, incluindo falhas.

    def _send_indicators_command(self, command):
        """
        Envia comandos relacionados à aba Indicadores para o EA associado à corretora selecionada.
        Lida com comandos single-shot de indicadores e streaming de dados OHLC+Indicadores.

        Args:
            command: Nome do comando a ser enviado (ex.: GET_INDICATOR_MA, START_STREAM_OHLC_INDICATORS).
        """
        broker_key = self.broker_combo.currentText()
        if not broker_key:
            self.update_log("Erro: Nenhuma corretora selecionada.")
            logger.warning("Nenhuma corretora selecionada ao tentar enviar comando.")
            self._update_buttons()
            return

        # Verifica se a corretora está registrada antes de enviar o comando
        is_registered = bool(
            broker_key in self.main_window.broker_status and self.main_window.broker_status[broker_key])
        if not is_registered and command in ["START_STREAM_OHLC_INDICATORS", "STOP_STREAM_OHLC_INDICATORS"]:
            self.update_log(f"Erro: Corretora {broker_key} não está registrada.")
            logger.warning(f"Comando {command} bloqueado: corretora {broker_key} não registrada.")
            self.streaming_active_by_broker[broker_key] = False
            self.stream_ohlc_indicators_request_ids[broker_key] = None
            self._update_buttons()
            return

        payload = {}
        try:
            if command == "START_STREAM_OHLC_INDICATORS":
                configs = []
                for row in range(self.stream_table.rowCount()):
                    symbol_item = self.stream_table.item(row, 0)
                    timeframe_item = self.stream_table.item(row, 1)
                    indicators_item = self.stream_table.item(row, 2)
                    # Verifica se todos os campos estão preenchidos e não vazios
                    if symbol_item and timeframe_item and indicators_item and \
                            symbol_item.text() and timeframe_item.text() and indicators_item.text():
                        symbol = symbol_item.text()
                        timeframe = timeframe_item.text()
                        indicators_str = indicators_item.text()
                        indicators = []
                        # Parseia a string de indicadores (ex: "MA,9;MA,21")
                        for ind_entry in indicators_str.split(';'):
                            if ind_entry:  # Garante que não é uma string vazia após split
                                try:
                                    type_, period = ind_entry.split(',')
                                    indicators.append({"type": type_.strip(), "period": int(period.strip())})
                                except ValueError:
                                    self.update_log(
                                        f"Erro: Formato inválido nos indicadores '{ind_entry}' da linha {row + 1}. Use 'TIPO,PERIODO'.")
                                    return
                            else:
                                self.update_log(f"Aviso: Indicador vazio na linha {row + 1}. Ignorando.")

                        if not indicators:  # Se a string de indicadores não gerou nenhum indicador válido
                            self.update_log(
                                f"Erro: Nenhum indicador válido encontrado na linha {row + 1}. Verifique o formato.")
                            return

                        configs.append({
                            "symbol": symbol.strip(),
                            "timeframe": timeframe.strip(),
                            "indicators": indicators
                        })
                    else:
                        self.update_log(
                            f"Aviso: Linha {row + 1} da tabela de streaming incompleta ou vazia. Ignorando.")

                if not configs:
                    self.update_log("Erro: Nenhuma configuração de streaming válida adicionada na tabela.")
                    return
                payload = {"configs": configs}
                # Gera o request_id para START_STREAM_OHLC_INDICATORS e o armazena para uso no STOP
                request_id = f"start_stream_ohlc_indicators_{broker_key}_{int(time.time())}"
                self.stream_ohlc_indicators_request_ids[broker_key] = request_id
                self.streaming_active_by_broker[broker_key] = True
                self.update_log(f"Iniciando streaming com request_id: {request_id} para {broker_key}")
                logger.info(f"Armazenado request_id para START_STREAM_OHLC_INDICATORS de {broker_key}: {request_id}")
                # Envia o comando e atualiza os botões
                asyncio.create_task(self.send_command(broker_key, command, payload, 'data', use_data_port=True))
                self._update_buttons()
            elif command == "STOP_STREAM_OHLC_INDICATORS":
                # Reutiliza o request_id armazenado do START_STREAM_OHLC_INDICATORS
                if broker_key in self.stream_ohlc_indicators_request_ids and self.stream_ohlc_indicators_request_ids[
                    broker_key]:
                    request_id = self.stream_ohlc_indicators_request_ids[broker_key]
                    self.update_log(f"Parando streaming com request_id: {request_id} para {broker_key}")
                    logger.info(
                        f"Reutilizando request_id para STOP_STREAM_OHLC_INDICATORS de {broker_key}: {request_id}")
                    # Passa o request_id armazenado explicitamente
                    asyncio.create_task(self.zmq_router.send_command_to_broker(
                        broker_key, command, payload, request_id, use_data_port=True))
                    # Limpa o estado do streaming
                    self.stream_ohlc_indicators_request_ids[broker_key] = None
                    self.streaming_active_by_broker[broker_key] = False
                    self._update_buttons()
                    self.update_log(f"Streaming parado para {broker_key}.")
                    logger.info(f"Streaming parado para {broker_key}.")
                else:
                    self.update_log(
                        f"Aviso: Nenhum streaming ativo encontrado para parar na corretora {broker_key}. Estado ajustado.")
                    logger.info(
                        f"Nenhum request_id armazenado para STOP_STREAM_OHLC_INDICATORS de {broker_key}. Ajustando estado.")
                    # Garante que o estado esteja consistente
                    self.streaming_active_by_broker[broker_key] = False
                    self.stream_ohlc_indicators_request_ids[broker_key] = None
                    self._update_buttons()
            elif command in ["GET_INDICATOR_MA", "GET_OHLC", "GET_TICK"]:
                symbol = self.indicator_symbol.text()
                if not symbol:
                    self.update_log("Erro: Símbolo vazio")
                    return
                payload = {"symbol": symbol}
                if command in ["GET_INDICATOR_MA", "GET_OHLC"]:
                    timeframe = self.indicator_timeframe.text()
                    if not timeframe:
                        self.update_log("Erro: Timeframe vazio")
                        return
                    payload["timeframe"] = timeframe
                if command == "GET_INDICATOR_MA":
                    period = int(self.indicator_period.text())
                    if period <= 0:
                        self.update_log("Erro: Período inválido")
                        return
                    payload["period"] = period
                asyncio.create_task(self.send_data_command(broker_key, command, payload))
        except Exception as e:
            self.update_log(f"❌ Erro ao processar comando {command}: {str(e)}")
            logger.error(f"Erro ao processar comando {command} para {broker_key}: {str(e)}")
            # Em caso de erro no START, reverte o estado do streaming
            if command == "START_STREAM_OHLC_INDICATORS":
                self.streaming_active_by_broker[broker_key] = False
                self.stream_ohlc_indicators_request_ids[broker_key] = None
                self._update_buttons()

# gui/mt5_trader_gui.py
# Versão 1.0.9.g - Correção Final