"""
Módulo main_window.py - Interface gráfica principal do MT5 ZMQ Trader

Este módulo implementa a janela principal da aplicação, responsável por:
- Exibir a lista de corretoras conectadas
- Mostrar logs e mensagens ZMQ
- Gerenciar o status das conexões
- Fornecer acesso às funcionalidades do sistema através de menus

A classe MainWindow atua como o ponto central da interface do usuário,
integrando os diversos componentes do sistema.
"""

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

# Configuração do logger para este módulo
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
    # Definição de sinais personalizados
    broker_status_updated = Signal(dict, dict)  # Sinal para atualização de status (status, modos)
    broker_connected = Signal(str)  # Sinal para quando uma corretora é conectada

    def __init__(self,
                 config: ConfigManager,
                 broker_manager: BrokerManager,
                 zmq_router: ZmqRouter,
                 shutdown_event_ref: asyncio.Event,
                 root_path: str):
        """
        Inicializa a janela principal da aplicação.

        Args:
            config: Gerenciador de configurações do sistema
            broker_manager: Gerenciador de corretoras
            zmq_router: Roteador ZMQ para comunicação com MT5
            shutdown_event_ref: Referência ao evento de encerramento global
            root_path: Caminho raiz da aplicação
        """
        super().__init__()
        # Armazena referências aos componentes principais
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.shutdown_event_ref = shutdown_event_ref
        self.root_path = root_path
        self.zmq_message_handler = ZmqMessageHandler(config, zmq_router, self)

        # Configuração da janela
        self.setWindowTitle("MT5 ZMQ Trader")
        self.setGeometry(100, 100, 900, 600)

        # Carrega informações das corretoras
        self.brokers = self.broker_manager.load_brokers()
        self.broker_status = {}  # Dicionário para armazenar status de cada corretora
        self.broker_modes = {}  # Dicionário para armazenar modos de operação

        # Inicializa os modos de operação das corretoras
        for key, broker in self.brokers.items():
            self.broker_modes[key] = broker.get("mode", "Hedge")
            logger.debug(f"Corretora {key} tem modo: {self.broker_modes[key]}")

        logger.info(f"Modos de corretoras carregados: {self.broker_modes}")

        # Inicializa a interface do usuário
        self._init_ui()
        self._connect_signals()
        self._create_menu()

        # Configura o monitor de internet
        self.internet_monitor = InternetMonitor(self._update_status_bar_timer)
        self.internet_monitor.start()

        # Configura o timer para atualização da barra de status
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_status_bar_timer)
        self.status_timer.start(5000)  # Atualiza a cada 5 segundos

        logger.info("MainWindow inicializada. ZmqRouter configurado para portas dinâmicas.")
        # Emite o sinal inicial de status das corretoras
        self.broker_status_updated.emit(self.broker_status, self.broker_modes)

    def _init_ui(self):
        """
        Inicializa os componentes da interface do usuário.

        Cria o layout principal, painéis, lista de corretoras, área de logs
        e barra de status.
        """
        # Widget central
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Layout principal com splitter horizontal
        main_layout = QVBoxLayout(central_widget)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # Painel esquerdo - Lista de corretoras
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        splitter.addWidget(left_panel)

        left_layout.addWidget(QLabel("Corretoras Conectadas:"))
        self.broker_list_widget = QListWidget()
        self.broker_list_widget.currentItemChanged.connect(self._on_broker_selected)
        left_layout.addWidget(self.broker_list_widget)

        # Painel direito - Logs e mensagens
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        splitter.addWidget(right_panel)

        right_layout.addWidget(QLabel("Logs e Mensagens ZMQ:"))
        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        right_layout.addWidget(self.log_text_edit)

        # Define o tamanho inicial dos painéis
        splitter.setSizes([300, 600])

        # Barra de status
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        # Indicadores na barra de status
        self.system_status_label = QLabel("Iniciando...")
        self.status_bar.addPermanentWidget(self.system_status_label, 1)

        self.internet_status_label = QLabel()
        self.status_bar.addPermanentWidget(self.internet_status_label)

    def showEvent(self, event):
        """
        Manipula o evento de exibição da janela.

        Args:
            event: Evento de exibição
        """
        super().showEvent(event)
        # Atualiza o status após um breve atraso
        QTimer.singleShot(500, lambda: self.system_status_label.setText("Pronto"))

    def closeEvent(self, event: QCloseEvent):
        """
        Manipula o evento de fechamento da janela.

        Sinaliza o encerramento da aplicação e para os componentes em execução.

        Args:
            event: Evento de fechamento
        """
        logger.info("Evento de fechamento da janela principal recebido.")
        # Sinaliza o evento de encerramento global
        self.shutdown_event_ref.set()
        # Para o monitor de internet e o timer de status
        self.internet_monitor.stop()
        self.status_timer.stop()
        event.accept()

    def _populate_brokers(self):
        """
        Preenche a lista de corretoras conectadas.

        Atualiza a interface com as corretoras atualmente conectadas
        e mantém o status de cada uma.
        """
        # Armazena as corretoras atualmente exibidas
        previous_brokers = set(self.broker_list_widget.item(i).text() for i in range(self.broker_list_widget.count()))
        self.broker_list_widget.clear()

        # Obtém as corretoras conectadas
        connected = self.broker_manager.get_connected_brokers()

        # Atualiza o status das corretoras desconectadas
        for key in list(self.broker_status.keys()):
            if key not in connected:
                self.broker_status[key] = False
                logger.info(f"Corretora {key} não está mais conectada. Status definido como False.")

        # Adiciona as corretoras conectadas à lista
        for key in sorted(connected):
            item = QListWidgetItem(key)
            self.broker_list_widget.addItem(item)

            # Inicializa o status se for uma nova corretora
            if key not in self.broker_status:
                self.broker_status[key] = False
                logger.info(f"Nova corretora {key} adicionada ao broker_status com status False.")

            # Emite sinal se for uma nova corretora
            if key not in previous_brokers:
                self.broker_connected.emit(key)

        # Atualiza a seleção atual
        self._on_broker_selected(self.broker_list_widget.currentItem(), None)
        # Emite sinal de atualização de status
        self.broker_status_updated.emit(self.broker_status, self.broker_modes)

    @Slot()
    def _update_brokers_list(self):
        """
        Atualiza a lista de corretoras.

        Slot conectado ao menu de conexão para atualizar a lista
        quando o menu é aberto.
        """
        self._populate_brokers()

    def _get_selected_broker_key(self) -> str | None:
        """
        Obtém a chave da corretora selecionada.

        Returns:
            str | None: Chave da corretora selecionada ou None se nenhuma estiver selecionada
        """
        current_item = self.broker_list_widget.currentItem()
        return current_item.text() if current_item else None

    def _connect_signals(self):
        """
        Conecta os sinais aos slots correspondentes.

        Estabelece as conexões entre os sinais emitidos pelo handler de mensagens ZMQ
        e os slots que atualizam a interface.
        """
        # Conecta o sinal de mensagem recebida aos slots de atualização
        self.zmq_message_handler.log_message_received.connect(self._update_log_display)
        self.zmq_message_handler.log_message_received.connect(self._handle_zmq_messages)

    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_broker_selected(self, current_item: QListWidgetItem | None, previous_item: QListWidgetItem | None):
        """
        Manipula a seleção de uma corretora na lista.

        Args:
            current_item: Item atualmente selecionado
            previous_item: Item previamente selecionado
        """
        selected_key = self._get_selected_broker_key()
        selected = selected_key is not None

        if selected:
            logger.debug(f"Corretora selecionada: {selected_key}. Conectado: True")
        else:
            logger.debug("Nenhuma corretora selecionada.")

    @Slot(str)
    def _update_log_display(self, message: str):
        """
        Atualiza o display de log com uma nova mensagem.

        Args:
            message: Mensagem a ser adicionada ao log
        """
        try:
            self.log_text_edit.append(message)
        except Exception as e:
            logger.error(f"Falha ao atualizar log display: {e}")

    @Slot(dict)
    def _update_status_bar_timer(self, status=None):
        """
        Atualiza a barra de status com informações do sistema.

        Args:
            status: Dicionário com informações de status (internet, cpu, memory)
        """
        if status is not None:
            message = f"Internet: {status['internet']} | {status['cpu']} | {status['memory']}"
            self.internet_status_label.setText(message)

    def _create_menu(self):
        """
        Cria o menu principal da aplicação.

        Configura o menu e conecta suas ações aos métodos correspondentes.
        """
        # Cria o menu principal
        self.main_menu = MainMenu(self, self.config, self.broker_manager, self.zmq_router)
        self.setMenuBar(self.main_menu.menubar)
        # Conecta o sinal de exibição do menu de conexão à atualização da lista
        self.main_menu.conn_menu.aboutToShow.connect(self._update_brokers_list)

        # Envolve o método de conexão para atualizar a lista após a conexão
        if hasattr(self.main_menu, "connect_broker"):
            self.main_menu.connect_broker = self._wrap_update(self.main_menu.connect_broker)

        # Envolve o método de desconexão para atualizar o status e a lista
        if hasattr(self.main_menu, "disconnect_broker"):
            original_disconnect = self.main_menu.disconnect_broker

            def wrapped_disconnect(*args, **kwargs):
                """
                Wrapper para o método de desconexão de corretora.

                Atualiza o status e a lista após a desconexão.
                """
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
        """
        Envolve uma função para adicionar uma atualização da lista após sua execução.

        Args:
            func: Função a ser envolvida

        Returns:
            function: Função envolvida com atualização da lista
        """

        def wrapper(*args, **kwargs):
            """
            Wrapper que executa a função original e atualiza a lista.
            """
            result = func(*args, **kwargs)
            self._update_brokers_list()
            return result

        return wrapper

    @Slot(str)
    def _handle_zmq_messages(self, message: str):
        """
        Processa mensagens ZMQ recebidas.

        Atualiza o status das corretoras com base nas mensagens recebidas.

        Args:
            message: Mensagem ZMQ recebida
        """
        status_changed = False

        # Verifica se a mensagem indica uma mudança de status
        for key in list(self.broker_status.keys()):
            # Verifica registro de corretora
            if "REGISTER" in message and key in message and "UNREGISTER" not in message:
                self.broker_status[key] = True
                logger.info(f"Corretora {key} registrada. Habilitando botões.")
                status_changed = True
                break
            # Verifica desregistro de corretora
            elif ("CLIENT_UNREGISTERED" in message or "UNREGISTER" in message) and key in message:
                self.broker_status[key] = False
                logger.info(f"Corretora {key} desregistrada. Desabilitando botões.")
                status_changed = True
                break

        # Emite sinal de atualização de status
        self.broker_status_updated.emit(self.broker_status, self.broker_modes)

# Arquivo: gui/main_window.py
# Versão: 1.0.9.d - Envio 1