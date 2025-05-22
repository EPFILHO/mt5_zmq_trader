import logging
import asyncio
import os
import time
from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QListWidget,
    QPushButton, QTextEdit, QLabel, QSplitter, QListWidgetItem, QStatusBar, QMessageBox
)
from PySide6.QtGui import QCloseEvent
from PySide6.QtCore import Slot, Qt, QTimer, Signal

from core.config_manager import ConfigManager
from core.broker_manager import BrokerManager
from core.zmq_router import ZmqRouter
from gui.main_menu import MainMenu
from gui.brokers_dialog import BrokersDialog
from core.zmq_message_handler import ZmqMessageHandler
from internet_monitor import InternetMonitor

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    broker_status_updated = Signal(dict, dict)
    broker_connected = Signal(str)

    def __init__(self,
                 config: ConfigManager,
                 broker_manager: BrokerManager,
                 zmq_router: ZmqRouter,
                 shutdown_event_ref: asyncio.Event,
                 root_path: str):
        super().__init__()
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.shutdown_event_ref = shutdown_event_ref
        self.root_path = root_path

        self.zmq_message_handler = ZmqMessageHandler(config, zmq_router, self)

        self.setWindowTitle("MT5 ZMQ Trader")
        self.setGeometry(100, 100, 900, 600)

        self.brokers = self.broker_manager.load_brokers()
        self.broker_status = {}
        self.broker_modes = {}

        for key, broker in self.brokers.items():
            self.broker_modes[key] = broker.get("mode", "Hedge")
            logger.debug(f"Corretora {key} tem modo: {self.broker_modes[key]}")

        logger.info(f"Modos de corretoras carregados: {self.broker_modes}")

        self._init_ui()
        self._connect_signals()
        self._create_menu()

        self.internet_monitor = InternetMonitor(self._update_status_bar_timer)
        self.internet_monitor.start()

        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_status_bar_timer)
        self.status_timer.start(5000)

        logger.info("MainWindow inicializada. ZmqRouter configurado para portas dinâmicas.")
        self.broker_status_updated.emit(self.broker_status, self.broker_modes)  # Emitir sinal após inicialização

    def _init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        splitter.addWidget(left_panel)
        left_layout.addWidget(QLabel("Corretoras Conectadas:"))
        self.broker_list_widget = QListWidget()
        self.broker_list_widget.currentItemChanged.connect(self._on_broker_selected)
        left_layout.addWidget(self.broker_list_widget)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        splitter.addWidget(right_panel)
        right_layout.addWidget(QLabel("Logs e Mensagens ZMQ:"))
        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        right_layout.addWidget(self.log_text_edit)

        splitter.setSizes([300, 600])

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.system_status_label = QLabel("Iniciando...")
        self.status_bar.addPermanentWidget(self.system_status_label, 1)
        self.internet_status_label = QLabel()
        self.status_bar.addPermanentWidget(self.internet_status_label)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(500, lambda: self.system_status_label.setText("Pronto"))

    def closeEvent(self, event: QCloseEvent):
        logger.info("Evento de fechamento da janela principal recebido.")
        self.shutdown_event_ref.set()
        self.internet_monitor.stop()
        self.status_timer.stop()
        event.accept()

    def _populate_brokers(self):
        previous_brokers = set(self.broker_list_widget.item(i).text() for i in range(self.broker_list_widget.count()))
        self.broker_list_widget.clear()
        connected = self.broker_manager.get_connected_brokers()
        for key in list(self.broker_status.keys()):
            if key not in connected:
                self.broker_status[key] = False
                logger.info(f"Corretora {key} não está mais conectada. Status definido como False.")
        for key in sorted(connected):
            item = QListWidgetItem(key)
            self.broker_list_widget.addItem(item)
            if key not in self.broker_status:
                self.broker_status[key] = False
                logger.info(f"Nova corretora {key} adicionada ao broker_status com status False.")
            if key not in previous_brokers:
                self.broker_connected.emit(key)
        self._on_broker_selected(self.broker_list_widget.currentItem(), None)
        self.broker_status_updated.emit(self.broker_status, self.broker_modes)

    @Slot()
    def _update_brokers_list(self):
        self._populate_brokers()

    def _get_selected_broker_key(self) -> str | None:
        current_item = self.broker_list_widget.currentItem()
        return current_item.text() if current_item else None

    def _connect_signals(self):
        self.zmq_message_handler.log_message_received.connect(self._update_log_display)
        self.zmq_message_handler.log_message_received.connect(self._handle_zmq_messages)

    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_broker_selected(self, current_item: QListWidgetItem | None, previous_item: QListWidgetItem | None):
        selected_key = self._get_selected_broker_key()
        selected = selected_key is not None
        if selected:
            logger.debug(f"Corretora selecionada: {selected_key}. Conectado: True")
        else:
            logger.debug("Nenhuma corretora selecionada.")

    @Slot(str)
    def _update_log_display(self, message: str):
        try:
            self.log_text_edit.append(message)
        except Exception as e:
            logger.error(f"Falha ao atualizar log display: {e}")

    @Slot(dict)
    def _update_status_bar_timer(self, status=None):
        if status is not None:
            message = f"Internet: {status['internet']} | {status['cpu']} | {status['memory']}"
            self.internet_status_label.setText(message)

    def _create_menu(self):
        self.main_menu = MainMenu(self, self.config, self.broker_manager, self.zmq_router)
        self.setMenuBar(self.main_menu.menubar)
        self.main_menu.conn_menu.aboutToShow.connect(self._update_brokers_list)
        if hasattr(self.main_menu, "connect_broker"):
            self.main_menu.connect_broker = self._wrap_update(self.main_menu.connect_broker)
        if hasattr(self.main_menu, "disconnect_broker"):
            original_disconnect = self.main_menu.disconnect_broker
            def wrapped_disconnect(*args, **kwargs):
                result = original_disconnect(*args, **kwargs)
                broker_key = args[0] if args else kwargs.get('broker_key')
                if broker_key in self.broker_status:
                    self.broker_status[broker_key] = False
                    logger.info(f"Corretora {broker_key} desconectada. Status atualizado para False.")
                    self.broker_status_updated.emit(self.broker_status, self.broker_modes)
                self._update_brokers_list()
                return result
            self.main_menu.disconnect_broker = wrapped_disconnect

    def _wrap_update(self, func):
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            self._update_brokers_list()
            return result
        return wrapper

    @Slot(str)
    def _handle_zmq_messages(self, message: str):
        status_changed = False
        for key in list(self.broker_status.keys()):
            if "REGISTER" in message and key in message and "UNREGISTER" not in message:
                self.broker_status[key] = True
                logger.info(f"Corretora {key} registrada. Habilitando botões.")
                status_changed = True
                break
            elif ("CLIENT_UNREGISTERED" in message or "UNREGISTER" in message) and key in message:
                self.broker_status[key] = False
                logger.info(f"Corretora {key} desregistrada. Desabilitando botões.")
                status_changed = True
                break
        if status_changed:
            self.broker_status_updated.emit(self.broker_status, self.broker_modes)
        else:
            self.broker_status_updated.emit(self.broker_status, self.broker_modes)

# Arquivo: gui/main_window.py
# Versão: 1.0.9.b - Envio 29