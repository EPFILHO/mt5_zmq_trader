# status_gui.py
# Versão 1.0.9.a - envio 5
# Ajuste: janela separada (QDialog), atualização automática sempre que aberta, "X" vermelho para MT5, só altera o estritamente necessário
# Novo: intervalo de atualização configurável via config.ini

import time
import logging
import asyncio
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QHeaderView
)
from PySide6.QtCore import Qt, Slot, QTimer, QEvent
from PySide6.QtGui import QFont, QBrush, QColor, QFontMetrics

logger = logging.getLogger(__name__)

class StatusGui(QDialog):
    def __init__(self, config, broker_manager, zmq_router, zmq_message_handler, main_window, parent=None):
        super().__init__(parent)
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.zmq_message_handler = zmq_message_handler
        self.main_window = main_window
        self.broker_data = {}
        self.setWindowTitle("Status das Corretoras")
        self.setModal(False)
        self._init_ui()
        self._connect_signals()
        self._adjust_table_size()
        self._setup_timer()
        self._timer_active = False

    def _init_ui(self):
        layout = QVBoxLayout(self)
        self.setLayout(layout)

        # Tabela: agora com 10 colunas (Balance de volta)
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            "MT5 Aberto", "MT5 Logado", "Algotrading", "EA Registrada",
            "Corretora - Conta", "Nome", "Tipo", "Modo", "Balance", "Latência"
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

        # Botão Atualizar (opcional, pode ser removido se quiser)
        update_button = QPushButton("Atualizar Agora")
        update_button.clicked.connect(self._update_periodically)
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

    def _connect_signals(self):
        if hasattr(self.main_window, "broker_connected"):
            self.main_window.broker_connected.connect(self.update_status)
        if hasattr(self.main_window, "broker_status_updated"):
            self.main_window.broker_status_updated.connect(self.update_status)
        self.zmq_message_handler.status_info_received.connect(self._update_status_info)
        logger.debug("Sinais conectados no StatusGui.")

    def _get_broker_info(self, key):
        brokers = self.broker_manager.get_brokers()
        return brokers.get(key, {})

    def _adjust_table_size(self):
        """Ajusta o tamanho da janela e da coluna 'Corretora - Conta'."""
        largura_fixa = 1200
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

    def _setup_timer(self):
        self.timer = QTimer(self)
        # Lê o intervalo de atualização do config.ini, com padrão de 5000 ms
        update_interval = self.config.getint('General', 'status_update_interval', fallback=5000)
        self.timer.setInterval(update_interval)
        self.timer.timeout.connect(self._update_periodically)
        logger.debug(f"Timer configurado com intervalo de {update_interval} ms")

    def showEvent(self, event):
        """Inicia o timer sempre que a janela for aberta/mostrada."""
        super().showEvent(event)
        if not self._timer_active:
            self.timer.start()
            self._timer_active = True
            self._update_periodically()  # Atualiza imediatamente ao abrir

    def hideEvent(self, event):
        """Para o timer ao esconder a janela (opcional, redundante com closeEvent)."""
        super().hideEvent(event)
        if self._timer_active:
            self.timer.stop()
            self._timer_active = False

    def closeEvent(self, event):
        if hasattr(self, "timer") and self._timer_active:
            self.timer.stop()
            self._timer_active = False
            logger.info("Timer parado ao fechar a janela StatusGui.")
        event.accept()

    def _send_periodic_commands(self):
        for key in self.broker_manager.get_connected_brokers():
            timestamp = int(time.time())
            payload = {"timestamp": time.time()}
            asyncio.create_task(self.zmq_router.send_command_to_broker(
                key, "GET_STATUS_INFO", payload, f"get_status_info_{key}_{timestamp}"
            ))
            logger.debug(f"Enviando GET_STATUS_INFO para {key}")

    @Slot()
    def _update_periodically(self):
        self._send_periodic_commands()
        self.update_status()
        logger.debug("Atualização periódica executada.")

    @Slot()
    def update_status(self):
        brokers = self.broker_manager.get_brokers()
        self._adjust_table_size()
        self.table.setRowCount(len(brokers))
        row = 0
        status_font = QFont()
        status_font.setPointSize(16)
        for key in sorted(brokers.keys()):
            is_connected = self.broker_manager.is_connected(key)
            # MT5 Aberto
            item = QTableWidgetItem("✔" if is_connected else "✘")
            item.setForeground(QBrush(QColor("green" if is_connected else "red")))
            item.setTextAlignment(Qt.AlignCenter)
            item.setFont(status_font)
            self.table.setItem(row, 0, item)

            # MT5 Logado
            is_registered = getattr(self.main_window, "broker_status", {}).get(key, False)
            if is_connected:
                symbol = "✔" if is_registered else "✘"
                color = "green" if is_registered else "red"
            else:
                symbol = "-"
                color = "black"
            item = QTableWidgetItem(symbol)
            item.setForeground(QBrush(QColor(color)))
            item.setTextAlignment(Qt.AlignCenter)
            item.setFont(status_font)
            self.table.setItem(row, 1, item)

            # Algotrading
            broker_data = self.broker_data.get(key, {})
            trade_allowed = broker_data.get("trade_allowed", None)
            if is_connected and is_registered:
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
            self.table.setItem(row, 2, item)

            # EA Registrada
            if is_connected and is_registered:
                symbol = "✔"
                color = "green"
            elif not is_connected:
                symbol = "-"
                color = "black"
            else:
                symbol = "✘"
                color = "red"
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

            # Balance
            balance = broker_data.get("balance", "-") if is_connected and is_registered else "-"
            self.table.setItem(row, 8, QTableWidgetItem(str(balance)))

            # Latência
            latency = broker_data.get("latency", "-") if is_connected and is_registered else "-"
            self.table.setItem(row, 9, QTableWidgetItem(latency))

            row += 1

        logger.debug(f"Tabela atualizada com {len(brokers)} corretoras.")

    @Slot(dict)
    def _update_status_info(self, response):
        broker_key = response.get("broker_key")
        if broker_key:
            self.broker_data.setdefault(broker_key, {})
            self.broker_data[broker_key]["trade_allowed"] = response.get("trade_allowed", None)
            self.broker_data[broker_key]["balance"] = response.get("balance", "-")
            self.broker_data[broker_key]["latency"] = response.get("latency", "-")
            self.update_status()

# ------------ término do arquivo status_gui.py ------------
# Versão 1.0.9.a - envio 5