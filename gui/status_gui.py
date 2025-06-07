# status_gui.py
# Versão 1.0.9.j - Envio 1
# Ajustes:
# - (1.0.9.j, envio 4): Adicionado uso do buffer trade_allowed_states do zmq_message_handler para inicializar e atualizar a coluna "Algotrading".
# - Simplificado _update_status_info e _update_trade_allowed para apenas chamar update_status, pois o buffer é atualizado pelo zmq_message_handler.
# - Mantidas todas as funcionalidades anteriores de 1.0.9.i, envio 3.

# Bloco 1 - Importações e Configuração Inicial
import time
import logging
import asyncio
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QHeaderView
)
from PySide6.QtCore import Qt, Slot, QTimer, QEvent
from PySide6.QtGui import QFont, QBrush, QColor, QFontMetrics

logger = logging.getLogger(__name__)

# Bloco 2 - Definição da Classe StatusGui
class StatusGui(QDialog):
    def __init__(self, config, broker_manager, zmq_router, zmq_message_handler, main_window, mt5_monitor, parent=None):
        """
        Inicializa a janela de status das corretoras.

        Args:
            config: Gerenciador de configurações.
            broker_manager: Gerenciador de corretoras.
            zmq_router: Roteador ZMQ para comunicação.
            zmq_message_handler: Manipulador de mensagens ZMQ.
            main_window: Referência à janela principal.
            mt5_monitor: Monitor de processos MT5.
            parent: Widget pai (opcional).
        """
        super().__init__(parent)
        logger.info("Bloco 2 - Inicializando StatusGui...")
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.zmq_message_handler = zmq_message_handler
        self.main_window = main_window
        self.mt5_monitor = mt5_monitor
        self.broker_data = {}  # Mantido por compatibilidade, mas menos usado agora
        self.setWindowTitle("Status das Corretoras")
        self.setModal(False)
        self._init_ui()
        self._connect_signals()
        self._adjust_table_size()
        logger.info("Bloco 2 - StatusGui inicializado.")

    # Bloco 3 - Configuração da Interface do Usuário
    def _init_ui(self):
        """Configura os elementos da interface gráfica."""
        logger.debug("Bloco 3 - Configurando interface do usuário...")
        layout = QVBoxLayout(self)
        self.setLayout(layout)

        # Tabela com 8 colunas
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "MT5 Aberto", "MT5 Conectado", "EA Registrada", "Algotrading",
            "Corretora - Conta", "Nome", "Tipo", "Modo"
        ])
        self.table.setRowCount(0)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #F5F5F5;
                border: 1px solid #CCCCCC;
                border-radius: 5px;
                gridline-color: #DDDDDD;
            }
            QTableWidget::item {
                padding: 5px;
            }
            QHeaderView::section {
                background-color: #E0E0E0;
                padding: 5px;
                border: 1px solid #CCCCCC;
            }
        """)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col in range(4):  # Centraliza colunas de status
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)

        # Botão Atualizar
        update_button = QPushButton("Atualizar Agora")
        update_button.clicked.connect(self.update_status)
        update_button.setStyleSheet("""
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
        layout.addWidget(update_button)
        logger.debug("Bloco 3 - Interface configurada.")

    # Bloco 4 - Conexão de Sinais
    def _connect_signals(self):
        """Conecta sinais para atualizar a tabela quando necessário."""
        logger.debug("Bloco 4 - Conectando sinais...")
        if hasattr(self.main_window, "broker_connected"):
            self.main_window.broker_connected.connect(self.update_status)
        if hasattr(self.main_window, "broker_status_updated"):
            self.main_window.broker_status_updated.connect(self.update_status)
        self.zmq_message_handler.status_info_received.connect(self._update_status_info)
        self.zmq_message_handler.trade_allowed_update_received.connect(self._update_trade_allowed)
        logger.debug("Bloco 4 - Sinais conectados.")

    # Bloco 5 - Ajuste de Tamanho da Tabela
    def _adjust_table_size(self):
        """Ajusta o tamanho da janela e da coluna 'Corretora - Conta'."""
        logger.debug("Bloco 5 - Ajustando tamanho da tabela...")
        largura_fixa = 1000
        self.setMinimumWidth(largura_fixa)
        self.resize(largura_fixa, self.height())

        num_brokers = len(self.broker_manager.get_brokers())
        row_height = 32
        header_height = 40
        margin = 60
        altura_total = header_height + (row_height * max(1, num_brokers)) + margin
        self.setMinimumHeight(altura_total)
        self.resize(self.width(), altura_total)

        broker_keys = list(self.broker_manager.get_brokers().keys())
        if broker_keys:
            font = QFont()
            font.setBold(True)
            metrics = QFontMetrics(font)
            max_key = max(broker_keys, key=len)
            width = metrics.horizontalAdvance(max_key + "  ") + 40
            col_idx = 4
            self.table.setColumnWidth(col_idx, width)
        else:
            self.table.setColumnWidth(4, 180)
        logger.debug("Bloco 5 - Tamanho da tabela ajustado.")

    # Bloco 7 - Gerenciamento de Eventos da Janela
    def showEvent(self, event):
        """Atualiza a tabela ao mostrar a janela."""
        logger.debug("Bloco 7 - Janela StatusGui exibida.")
        super().showEvent(event)
        self.update_status()

    def hideEvent(self, event):
        """Evento ao esconder a janela."""
        logger.debug("Bloco 7 - Janela StatusGui oculta.")
        super().hideEvent(event)

    def closeEvent(self, event):
        """Evento ao fechar a janela."""
        logger.info("Bloco 7 - Fechando janela StatusGui.")
        event.accept()

    # Bloco 11 - Atualização de Informações de Status
    @Slot(dict)
    def _update_status_info(self, response):
        """Atualiza informações de status recebidas do GET_STATUS_INFO."""
        logger.debug("Bloco 11 - Processando resposta de status...")
        self.update_status()
        logger.debug("Bloco 11 - Status atualizado.")

    @Slot(dict)
    def _update_trade_allowed(self, response):
        """Atualiza o status de trade_allowed recebido do evento TRADE_ALLOWED_UPDATE."""
        logger.debug("Bloco 11 - Processando evento TRADE_ALLOWED_UPDATE...")
        self.update_status()
        logger.debug("Bloco 11 - Trade_allowed atualizado.")

    # Bloco 10 - Atualização da Tabela de Status
    @Slot()
    def update_status(self):
        """Atualiza a tabela com o status das corretoras."""
        logger.debug("Bloco 10 - Atualizando tabela de status...")
        brokers = self.broker_manager.get_brokers()
        trade_allowed_states = self.zmq_message_handler.get_trade_allowed_states()  # Consultar o buffer
        self._adjust_table_size()
        self.table.setRowCount(len(brokers))
        row = 0
        status_font = QFont()
        status_font.setPointSize(16)
        for key in sorted(brokers.keys()):
            # MT5 Aberto: Verifica se o processo MT5 está em execução
            process = self.broker_manager.mt5_processes.get(key, None)
            is_process_running = process is not None and process.poll() is None
            item = QTableWidgetItem("✔" if is_process_running else "✘")
            item.setForeground(QBrush(QColor("green" if is_process_running else "red")))
            item.setTextAlignment(Qt.AlignCenter)
            item.setFont(status_font)
            self.table.setItem(row, 0, item)

            # MT5 Conectado: Usa is_connected do BrokerManager
            is_connected = self.broker_manager.is_connected(key)
            if is_process_running:
                symbol = "✔" if is_connected else "✘"
                color = "green" if is_connected else "red"
            else:
                symbol = "-"
                color = "black"
            item = QTableWidgetItem(symbol)
            item.setForeground(QBrush(QColor(color)))
            item.setTextAlignment(Qt.AlignCenter)
            item.setFont(status_font)
            self.table.setItem(row, 1, item)

            # EA Registrada: Usa is_registered do main_window.broker_status
            is_registered = getattr(self.main_window, "broker_status", {}).get(key, False)
            if is_process_running:
                symbol = "✔" if is_registered else "✘"
                color = "green" if is_registered else "red"
            else:
                symbol = "-"
                color = "black"
            item = QTableWidgetItem(symbol)
            item.setForeground(QBrush(QColor(color)))
            item.setTextAlignment(Qt.AlignCenter)
            item.setFont(status_font)
            self.table.setItem(row, 2, item)

            # Algotrading: Consultar o buffer do zmq_message_handler
            if is_process_running and is_registered:
                trade_allowed = trade_allowed_states.get(key, None)
                if trade_allowed is True:
                    symbol = "✔"
                    color = "green"
                elif trade_allowed is False:
                    symbol = "✘"
                    color = "red"
                else:
                    symbol = "-"
                    color = "black"
            else:
                symbol = "-"
                color = "black"
            item = QTableWidgetItem(symbol)
            item.setForeground(QBrush(QColor(color)))
            item.setTextAlignment(Qt.AlignCenter)
            item.setFont(status_font)
            self.table.setItem(row, 3, item)

            # Corretora - Conta
            item = QTableWidgetItem(key)
            font = QFont()
            font.setBold(True)
            item.setFont(font)
            self.table.setItem(row, 4, item)

            # Nome
            broker_info = self._get_broker_info(key)
            client = broker_info.get("client", "N/A")
            self.table.setItem(row, 5, QTableWidgetItem(client))

            # Tipo
            account_type = broker_info.get("type", "N/A")
            self.table.setItem(row, 6, QTableWidgetItem(account_type))

            # Modo
            mode = broker_info.get("mode", "N/A")
            self.table.setItem(row, 7, QTableWidgetItem(mode))

            row += 1

        logger.debug(f"Bloco 10 - Tabela atualizada com {len(brokers)} corretoras.")

    def _get_broker_info(self, key):
        brokers = self.broker_manager.get_brokers()
        return brokers.get(key, {})

# ------------ término do arquivo status_gui.py ------------
# Versão 1.0.9.j - Envio 1