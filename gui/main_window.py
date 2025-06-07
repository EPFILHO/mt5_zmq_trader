# gui/main_window.py
# Versão 1.0.9.i - envio 5
# Ajustes:
# - (1.0.9.h): Estrutura inicial com menu, lista de corretoras e logs.
# - (1.0.9.i): Adicionado mt5_monitor no construtor para passar ao MainMenu e StatusGui.
# - (envio 5): Restaurado layout original com painel de logs (log_text_edit) que foi removido indevidamente.

# Bloco 1 - Configuração Inicial da Classe MainWindow
# Objetivo: Definir a classe principal da janela, seus sinais e inicializar atributos essenciais.
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
    """
    Janela principal da aplicação MT5 ZMQ Trader.

    Gerencia a interface do usuário, exibe informações sobre corretoras conectadas,
    logs de sistema e fornece acesso às funcionalidades através de menus.

    Sinais:
        broker_status_updated: Emitido quando o status de uma corretora muda
        broker_connected: Emitido quando uma nova corretora é conectada
    """
    broker_status_updated = Signal(dict, dict)
    broker_connected = Signal(str)

    def __init__(self,
                 config: ConfigManager,
                 broker_manager: BrokerManager,
                 zmq_router: ZmqRouter,
                 shutdown_event_ref: asyncio.Event,
                 root_path: str,
                 mt5_monitor):
        """
        Inicializa a janela principal da aplicação.

        Args:
            config: Gerenciador de configurações do sistema
            broker_manager: Gerenciador de corretoras
            zmq_router: Roteador ZMQ para comunicação com MT5
            shutdown_event_ref: Referência ao evento de encerramento global
            root_path: Caminho raiz da aplicação
            mt5_monitor: Monitor de processos MT5
        """
        super().__init__()
        logger.info("Bloco 1 - Inicializando MainWindow...")
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.shutdown_event_ref = shutdown_event_ref
        self.root_path = root_path
        self.mt5_monitor = mt5_monitor
        self.zmq_message_handler = ZmqMessageHandler(config, zmq_router, self)

        # Configuração da janela
        self.setWindowTitle("MT5 ZMQ Trader")
        self.setGeometry(100, 100, 900, 600)

        # Carrega informações das corretoras
        self.brokers = self.broker_manager.load_brokers()
        self.broker_status = {}
        self.broker_modes = {}

        # Inicializa os modos de operação das corretoras
        for key, broker in self.brokers.items():
            self.broker_modes[key] = broker.get("mode", "Hedge")
            logger.debug(f"Bloco 1 - Corretora {key} tem modo: {self.broker_modes[key]}")

        logger.info(f"Bloco 1 - Modos de corretoras carregados: {self.broker_modes}")

        # Inicializa a interface do usuário
        self._init_ui()
        self._connect_signals()
        self._create_menu()

        # Bloco 6 - Monitoramento e Barra de Status
        self.internet_monitor = InternetMonitor(self._update_status_bar_timer)
        self.internet_monitor.start()
        logger.info("Bloco 6 - InternetMonitor iniciado.")

        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_status_bar_timer)
        self.status_timer.start(5000)
        logger.info("Bloco 6 - Timer de status iniciado.")

        logger.info("Bloco 1 - MainWindow inicializada. ZmqRouter configurado para portas dinâmicas.")
        self.broker_status_updated.emit(self.broker_status, self.broker_modes)
        logger.debug("Bloco 1 - Sinal broker_status_updated emitido na inicialização.")

    # Bloco 2 - Inicialização da Interface do Usuário (_init_ui)
    def _init_ui(self):
        """
        Inicializa os componentes da interface do usuário.

        Cria o layout principal, painéis, lista de corretoras, área de logs
        e barra de status.
        """
        logger.info("Bloco 2 - Configurando a interface do usuário...")
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
        logger.info("Bloco 2 - Interface do usuário configurada.")

    # Bloco 3 - Gerenciamento de Eventos da Janela
    def showEvent(self, event):
        super().showEvent(event)
        logger.info("Bloco 3 - Janela principal exibida.")
        QTimer.singleShot(500, lambda: self.system_status_label.setText("Pronto"))

    def closeEvent(self, event: QCloseEvent):
        logger.info("Bloco 3 - Evento de fechamento da janela principal recebido.")
        self.shutdown_event_ref.set()
        logger.debug("Bloco 3 - shutdown_event_ref setado.")
        self.internet_monitor.stop()
        self.status_timer.stop()
        logger.info("Bloco 3 - InternetMonitor e status_timer parados.")
        event.accept()

    # Bloco 4 - Gerenciamento da Lista de Corretoras
    def _populate_brokers(self):
        logger.info("Bloco 4 - Populando lista de corretoras...")
        previous_brokers = set(self.broker_list_widget.item(i).text() for i in range(self.broker_list_widget.count()))
        self.broker_list_widget.clear()

        connected = self.broker_manager.get_connected_brokers()

        for key in list(self.broker_status.keys()):
            if key not in connected:
                self.broker_status[key] = False
                logger.info(f"Bloco 4 - Corretora {key} não está mais conectada. Status definido como False.")

        for key in sorted(connected):
            item = QListWidgetItem(key)
            self.broker_list_widget.addItem(item)

            if key not in self.broker_status:
                self.broker_status[key] = False
                logger.info(f"Bloco 4 - Nova corretora {key} adicionada ao broker_status com status False.")

            if key not in previous_brokers:
                self.broker_connected.emit(key)
                logger.debug(f"Bloco 4 - Sinal broker_connected emitido para {key}.")

        self._on_broker_selected(self.broker_list_widget.currentItem(), None)
        self.broker_status_updated.emit(self.broker_status, self.broker_modes)
        logger.info("Bloco 4 - Lista de corretoras populada e sinal broker_status_updated emitido.")

    @Slot()
    def _update_brokers_list(self):
        logger.info("Bloco 4 - Solicitando atualização da lista de corretoras.")
        self._populate_brokers()

    def _get_selected_broker_key(self) -> str | None:
        current_item = self.broker_list_widget.currentItem()
        selected_key = current_item.text() if current_item else None
        logger.debug(f"Bloco 4 - Corretora selecionada: {selected_key}.")
        return selected_key

    # Bloco 5 - Conexão de Sinais e Manipulação de Mensagens ZMQ
    def _connect_signals(self):
        logger.info("Bloco 5 - Conectando sinais...")
        self.zmq_message_handler.log_message_received.connect(self._update_log_display)
        self.zmq_message_handler.log_message_received.connect(self._handle_zmq_messages)
        logger.info("Bloco 5 - Sinais conectados.")

    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_broker_selected(self, current_item: QListWidgetItem | None, previous_item: QListWidgetItem | None):
        selected_key = self._get_selected_broker_key()
        if selected_key:
            logger.debug(f"Bloco 5 - Corretora selecionada na lista: {selected_key}.")
        else:
            logger.debug("Bloco 5 - Nenhuma corretora selecionada na lista.")

    @Slot(str)
    def _update_log_display(self, message: str):
        try:
            self.log_text_edit.append(message)
            logger.debug(f"Bloco 5 - Log display atualizado com mensagem: {message[:50]}...")
        except Exception as e:
            logger.error(f"Bloco 5 - Falha ao atualizar log display: {e}")

    @Slot(str)
    def _handle_zmq_messages(self, message: str):
        status_changed = False
        for key in list(self.broker_status.keys()):
            if "REGISTER" in message and key in message and "UNREGISTER" not in message:
                self.broker_status[key] = True
                logger.info(f"Bloco 5 - Corretora {key} registrada. Habilitando botões.")
                status_changed = True
                break
            elif ("CLIENT_UNREGISTERED" in message or "UNREGISTER" in message) and key in message:
                self.broker_status[key] = False
                logger.info(f"Bloco 5 - Corretora {key} desregistrada. Desabilitando botões.")
                status_changed = True
                break

        if status_changed:
            self.broker_status_updated.emit(self.broker_status, self.broker_modes)
            logger.debug("Bloco 5 - Sinal broker_status_updated emitido após handle_zmq_messages.")

    # Bloco 6 - Monitoramento e Barra de Status
    @Slot(dict)
    def _update_status_bar_timer(self, status=None):
        if status is not None:
            message = f"Internet: {status['internet']} | {status['cpu']} | {status['memory']}"
            self.internet_status_label.setText(message)
            logger.debug(f"Bloco 6 - Barra de status atualizada: {message}.")

    # Bloco 7 - Criação e Interação do Menu Principal
    def _create_menu(self):
        logger.info("Bloco 7 - Criando menu principal...")
        self.main_menu = MainMenu(self, self.config, self.broker_manager, self.zmq_router, self.mt5_monitor)
        self.setMenuBar(self.main_menu.menubar)
        self.main_menu.conn_menu.aboutToShow.connect(self._update_brokers_list)
        logger.debug("Bloco 7 - Menu 'Conexões' conectado ao _update_brokers_list.")

        if hasattr(self.main_menu, "connect_broker"):
            self.main_menu.connect_broker = self._wrap_update(self.main_menu.connect_broker, "connect")
            logger.debug("Bloco 7 - Método connect_broker envolvido.")

        if hasattr(self.main_menu, "disconnect_broker"):
            original_disconnect = self.main_menu.disconnect_broker

            def wrapped_disconnect(*args, **kwargs):
                logger.info("Bloco 7 - Executando wrapped_disconnect.")
                result = original_disconnect(*args, **kwargs)
                broker_key = args[0] if args else kwargs.get('broker_key')

                if broker_key in self.broker_status:
                    self.broker_status[broker_key] = False
                    logger.info(f"Bloco 7 - Corretora {broker_key} desconectada. Status atualizado para False.")

                self.broker_status_updated.emit(self.broker_status, self.broker_modes)
                logger.debug("Bloco 7 - Sinal broker_status_updated emitido após desconexão.")
                self._update_brokers_list()
                return result

            self.main_menu.disconnect_broker = wrapped_disconnect
            logger.debug("Bloco 7 - Método disconnect_broker envolvido.")
        logger.info("Bloco 7 - Menu principal criado e configurado.")

    def _wrap_update(self, func, action_type: str):
        def wrapper(*args, **kwargs):
            logger.info(f"Bloco 7 - Executando wrapper para {action_type} broker.")
            result = func(*args, **kwargs)
            self._update_brokers_list()
            return result
        return wrapper

# ------------ término do arquivo main_window.py ------------
# Versão 1.0.9.i - envio 5