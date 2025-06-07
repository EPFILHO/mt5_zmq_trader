# gui/main_menu.py
# Versão 1.0.9.i - envio 1
# Ajustes:
# - (1.0.9.b): Adicionada ação "Boleta de Trades" no menu "Ferramentas" para abrir BoletaTraderGui.
# - (1.0.9.i): Adicionado mt5_monitor no construtor e passado ao StatusGui em open_status_window.
# - [FIX 2] Remove o argumento 'key' das chamadas a _populate_broker_tabs da BoletaTraderGui.

import logging
from PySide6.QtWidgets import QMenu, QMessageBox, QMenuBar
from PySide6.QtCore import Slot, QCoreApplication
from gui.brokers_dialog import BrokersDialog
from gui.commands_dialog import CommandsDialog
from gui.status_gui import StatusGui
from gui.mt5_trader_gui import MT5TraderGui
from gui.boleta_trader_gui import BoletaTraderGui

logger = logging.getLogger(__name__)

class MainMenu:
    def __init__(self, main_window, config, broker_manager, zmq_router, mt5_monitor):
        self.main_window = main_window
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.mt5_monitor = mt5_monitor  # Novo: armazenar mt5_monitor
        self.menubar = QMenuBar()
        self.conn_menu = None
        self._brokers_dialog = None
        self._commands_dialog = None
        self._status_dialog = None
        self._trader_dialog = None
        self._boleta_dialog = None
        self._create_menus()
        logger.info("Classe MainMenu inicializada.")

    def _create_menus(self):
        self._create_config_menu()
        self._create_conn_menu()
        self._create_tools_menu()
        self._create_exit_menu()
        logger.debug("Menus criados.")

    def _create_config_menu(self):
        config_menu = QMenu("Configurações", self.menubar)
        self.menubar.addMenu(config_menu)
        brokers_menu = QMenu("Corretoras", config_menu)
        config_menu.addMenu(brokers_menu)
        cadastro_action = brokers_menu.addAction("Cadastro")
        cadastro_action.triggered.connect(self.open_cadastro_window)
        logger.debug("Menu Configurações criado.")

    def _create_conn_menu(self):
        self.conn_menu = QMenu("Conexões", self.menubar)
        self.menubar.addMenu(self.conn_menu)
        self._populate_conn_menu()
        logger.debug("Menu Conexões criado.")

    def _create_tools_menu(self):
        """Cria o menu de ferramentas."""
        tools_menu = QMenu("Ferramentas", self.menubar)
        self.menubar.addMenu(tools_menu)
        boleta_action = tools_menu.addAction("Boleta de Comandos")
        boleta_action.triggered.connect(self.open_commands_window)
        status_action = tools_menu.addAction("Status das Corretoras")
        status_action.triggered.connect(self.open_status_window)
        trader_action = tools_menu.addAction("Trader GUI")
        trader_action.triggered.connect(self.open_trader_window)
        boleta_trades_action = tools_menu.addAction("Boleta de Trades")
        boleta_trades_action.triggered.connect(self.open_boleta_window)
        logger.debug("Menu Ferramentas criado.")

    def _create_exit_menu(self):
        exit_action = self.menubar.addAction("Sair")
        exit_action.triggered.connect(self.quit)
        logger.debug("Menu Sair criado.")

    def open_cadastro_window(self):
        """Abre a janela de cadastro de corretoras."""
        if self._brokers_dialog is None:
            self._brokers_dialog = BrokersDialog(self.config, self.broker_manager, self.main_window)
        if hasattr(self.main_window, "_update_brokers_list"):
            self._brokers_dialog.brokers_updated.connect(self.main_window._update_brokers_list)
        self._brokers_dialog.show()
        self._brokers_dialog.raise_()
        self._brokers_dialog.activateWindow()

    def open_commands_window(self):
        """Abre a janela de comandos."""
        if self._commands_dialog is None:
            self._commands_dialog = CommandsDialog(self.config, self.broker_manager, self.zmq_router,
                                                   self.main_window.zmq_message_handler, self.main_window)
        self._commands_dialog.show()
        self._commands_dialog.raise_()
        self._commands_dialog.activateWindow()

    def open_status_window(self):
        """Abre a janela de status das corretoras."""
        if self._status_dialog is None:
            self._status_dialog = StatusGui(self.config, self.broker_manager, self.zmq_router,
                                            self.main_window.zmq_message_handler, self.main_window, self.mt5_monitor)
        self._status_dialog.show()
        self._status_dialog.raise_()
        self._status_dialog.activateWindow()

    def open_trader_window(self):
        """Abre a janela de trader GUI."""
        if self._trader_dialog is None:
            self._trader_dialog = MT5TraderGui(self.config, self.broker_manager, self.zmq_router,
                                               self.main_window.zmq_message_handler, self.main_window)
        self._trader_dialog.show()
        self._trader_dialog.raise_()
        self._trader_dialog.activateWindow()

    def open_boleta_window(self):
        """Abre a janela de boleta de trades."""
        if self._boleta_dialog is None:
            self._boleta_dialog = BoletaTraderGui(self.config, self.broker_manager, self.zmq_router,
                                                  self.main_window.zmq_message_handler, self.main_window)
        self._boleta_dialog.show()
        self._boleta_dialog.raise_()
        self._boleta_dialog.activateWindow()
        logger.info("Janela Boleta de Trades aberta.")

    @Slot()
    def quit(self):
        """Encerra a aplicação de forma controlada."""
        logger.info("Encerrando a aplicação via menu 'Sair'.")
        if hasattr(self.main_window, "shutdown_event_ref"):
            self.main_window.shutdown_event_ref.set()
        QCoreApplication.quit()

    def _populate_conn_menu(self):
        """Preenche o menu de conexões com as corretoras."""
        if self.conn_menu is None:
            logger.warning("Tentativa de atualizar menu Conexões antes da criação.")
            return
        self.conn_menu.clear()
        brokers = self.broker_manager.get_brokers()
        connect_menu = QMenu("Conectar", self.conn_menu)
        disconnect_menu = QMenu("Desconectar", self.conn_menu)
        for key in sorted(brokers.keys()):
            if not self.broker_manager.is_connected(key):
                action = connect_menu.addAction(key)
                action.triggered.connect(lambda checked=False, k=key: self.connect_broker(k))
            else:
                action = disconnect_menu.addAction(key)
                action.triggered.connect(lambda checked=False, k=key: self.disconnect_broker(k))
        if connect_menu.isEmpty():
            connect_menu.addAction("Vazio").setEnabled(False)
        if disconnect_menu.isEmpty():
            disconnect_menu.addAction("Vazio").setEnabled(False)
        self.conn_menu.addMenu(connect_menu)
        self.conn_menu.addMenu(disconnect_menu)
        logger.debug(f"Status de conexão das corretoras: {self.broker_manager.connected_brokers}")

    @Slot()
    def connect_broker(self, key):
        """Conecta uma corretora."""
        if self.broker_manager.connect_broker(key):
            logger.info(f"Conectando corretora: {key}")
            self._populate_conn_menu()
            if hasattr(self, "_commands_dialog") and self._commands_dialog is not None:
                self._commands_dialog.update_brokers()
            if hasattr(self, "_status_dialog") and self._status_dialog is not None:
                self._status_dialog.update_status()
            if hasattr(self, "_trader_dialog") and self._trader_dialog is not None:
                self._trader_dialog._populate_brokers()
            # [FIX 2] Remove o argumento 'key' da chamada. A seleção será tratada no _on_broker_status_updated.
            if hasattr(self, "_boleta_dialog") and self._boleta_dialog is not None:
                self._boleta_dialog._populate_broker_tabs()
        else:
            logger.error(f"Falha ao conectar corretora: {key}")

    @Slot()
    def disconnect_broker(self, key):
        """Desconecta uma corretora."""
        if self.broker_manager.disconnect_broker(key):
            logger.info(f"Desconectando corretora: {key}")
            self._populate_conn_menu()
            if hasattr(self, "_commands_dialog") and self._commands_dialog is not None:
                self._commands_dialog.update_brokers()
            if hasattr(self, "_status_dialog") and self._status_dialog is not None:
                self._status_dialog.update_status()
            if hasattr(self, "_trader_dialog") and self._trader_dialog is not None:
                self._trader_dialog._populate_brokers()
            # [FIX 2] Remove o argumento 'key' da chamada. A seleção será tratada no _on_broker_status_updated.
            if hasattr(self, "_boleta_dialog") and self._boleta_dialog is not None:
                self._boleta_dialog._populate_broker_tabs()
        else:
            logger.error(f"Falha ao desconectar corretora: {key}")

# ------------ término do arquivo gui/main_menu.py ------------
# Versão 1.0.9.i - envio 1