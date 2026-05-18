# EPCopyFlow 2.0 - Versão 0.0.1 - Claude Code Parte 000
# gui/main_window.py
# Janela principal com sidebar de navegação e header com monitor de sistema.

import logging
from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QLabel,
    QPushButton, QStackedWidget, QFrame, QSizePolicy, QMessageBox
)
from PySide6.QtGui import QCloseEvent, QFont, QIcon
from PySide6.QtCore import Slot, Qt, Signal, QTimer

from core.config_manager import ConfigManager
from core.broker_manager import BrokerManager
from core.tcp_router import TcpRouter
from core.tcp_message_handler import TcpMessageHandler
from core.version import __version__
from internet_monitor import InternetMonitor
from gui import themes

from gui.pages.dashboard_page import DashboardPage
from gui.pages.brokers_page import BrokersPage
from gui.pages.history_page import HistoryPage
from gui.pages.logs_page import LogsPage
from gui.pages.settings_page import SettingsPage
from gui.widgets.notification_center import NotificationCenter, NotificationLevel

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    broker_status_updated = Signal(dict, dict)
    broker_connected = Signal(str)

    def __init__(self,
                 config: ConfigManager,
                 broker_manager: BrokerManager,
                 tcp_router: TcpRouter,
                 engine,
                 root_path: str,
                 mt5_monitor,
                 copytrade_manager,
                 tcp_message_handler: TcpMessageHandler):
        """
        engine: core.engine_thread.EngineThread — usado para submeter
        coroutines (emergency close, shutdown) ao loop do motor.
        tcp_message_handler: construído no bootstrap do motor (na thread do
        motor) e injetado aqui para garantir que QObjects emissores nasçam
        com a thread affinity correta. Ver issue #111.
        """
        super().__init__()
        self.config = config
        self.broker_manager = broker_manager
        self.tcp_router = tcp_router
        self.engine = engine
        self.root_path = root_path
        self.mt5_monitor = mt5_monitor
        self.copytrade_manager = copytrade_manager
        self.tcp_message_handler = tcp_message_handler

        self.brokers = self.broker_manager.load_brokers()
        self.broker_status = {}
        self.broker_modes = {}
        for key, broker in self.brokers.items():
            self.broker_status[key] = False
            self.broker_modes[key] = broker.get("mode", "Hedge")

        # Carregar tema salvo
        saved_theme = self.config.get('GUI', 'theme', fallback='Escuro')
        themes.set_theme(saved_theme)

        self.setWindowTitle("EPCopyFlow 2.0")
        # 1400x800 acomoda 5 cards de 220px por linha (sidebar 200 + padding 48
        # + grid spacing) com folga. 900 mínimo mantém o app utilizável; com
        # janela menor sobra scroll horizontal mas tudo segue visível.
        self.setGeometry(50, 50, 1400, 800)
        self.setMinimumSize(900, 550)

        self._init_ui()
        self._connect_signals()

        # Internet monitor (QTimer-based, runs in GUI thread - thread-safe)
        self.internet_monitor = InternetMonitor(check_interval=5, parent=self)
        self.internet_monitor.status_updated.connect(self._on_system_status)
        self.internet_monitor.start()

        # Timer para polling periódico dos indicadores de status (detecta MT5 fechando)
        self.indicators_timer = QTimer(self)
        self.indicators_timer.timeout.connect(self._update_all_indicators)
        self.indicators_timer.start(2000)  # 2 segundos

        logger.info("MainWindow inicializada.")

    # ── UI Setup ──
    def _init_ui(self):
        central = QWidget()
        central.setObjectName("main-area")
        central.setStyleSheet(themes.main_area_style())
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Header
        self.header = self._create_header()
        root_layout.addWidget(self.header)

        # Body = sidebar + stacked pages
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.sidebar = self._create_sidebar()
        body_layout.addWidget(self.sidebar)

        self.pages = QStackedWidget()
        self.pages.setStyleSheet(themes.page_background_style())

        # Create pages
        self.dashboard_page = DashboardPage(
            self.broker_manager, self.copytrade_manager,
            tcp_message_handler=self.tcp_message_handler,
            mt5_monitor=self.mt5_monitor,
        )
        self.dashboard_page.set_broker_status(self.broker_status)
        self.brokers_page = BrokersPage(
            self.config, self.broker_manager, self.tcp_router, self.mt5_monitor,
            tcp_message_handler=self.tcp_message_handler
        )
        self.brokers_page.set_broker_status(self.broker_status)
        self.history_page = HistoryPage(self.copytrade_manager)
        self.logs_page = LogsPage()
        self.settings_page = SettingsPage(self.config, on_theme_changed=self.apply_theme)

        self.pages.addWidget(self.dashboard_page)   # 0
        self.pages.addWidget(self.brokers_page)      # 1
        self.pages.addWidget(self.history_page)      # 2
        self.pages.addWidget(self.logs_page)         # 3
        self.pages.addWidget(self.settings_page)     # 4

        body_layout.addWidget(self.pages, 1)
        root_layout.addWidget(body, 1)

        # Select dashboard by default
        self.nav_buttons[0].setChecked(True)

    def _create_header(self):
        header = QFrame()
        header.setObjectName("header")
        header.setStyleSheet(themes.header_style())
        header.setFixedHeight(52)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 4, 16, 4)

        title = QLabel("EPCopyFlow 2.0")
        title.setProperty("class", "header-title")
        layout.addWidget(title)

        layout.addStretch()

        # Centro de notificações (oculto quando não há notificações)
        self.notification_center = NotificationCenter(self)
        layout.addWidget(self.notification_center)

        layout.addStretch()

        # System status labels
        self.internet_label = QLabel("Internet: --")
        self.internet_label.setProperty("class", "header-status")
        self.cpu_label = QLabel("CPU: --%")
        self.cpu_label.setProperty("class", "header-status")
        self.cpu_label.setFixedWidth(90)
        self.mem_label = QLabel("RAM: --%")
        self.mem_label.setProperty("class", "header-status")
        self.mem_label.setFixedWidth(90)

        for lbl in (self.internet_label, self.cpu_label, self.mem_label):
            layout.addWidget(lbl)
            layout.addSpacing(12)

        # Emergency button
        self.emergency_btn = QPushButton("EMERGENCIA - Fechar Tudo")
        self.emergency_btn.setObjectName("emergency-btn")
        self.emergency_btn.clicked.connect(self._on_emergency)
        layout.addWidget(self.emergency_btn)

        return header

    def _create_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setStyleSheet(themes.sidebar_style())
        sidebar.setFixedWidth(200)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(2)

        pages = [
            ("Dashboard", 0),
            ("Corretoras", 1),
            ("Historico", 2),
            ("Logs", 3),
            ("Configuracoes", 4),
        ]

        self.nav_buttons = []
        for label, index in pages:
            btn = QPushButton(label)
            btn.setProperty("class", "nav-btn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, idx=index: self._navigate(idx))
            layout.addWidget(btn)
            self.nav_buttons.append(btn)

        layout.addStretch()

        # Version label at bottom
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setStyleSheet(themes.version_label_style())
        layout.addWidget(self.version_label)

        return sidebar

    def _navigate(self, index):
        self.pages.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)
        # Histórico (índice 2): carrega ao abrir a aba — evita ter que clicar
        # "Atualizar" toda vez que o programa inicia.
        if index == 2:
            self.history_page.refresh()

    # ── Theme ──
    def apply_theme(self):
        """Reaplica todos os estilos após troca de tema."""
        from PySide6.QtWidgets import QApplication
        QApplication.instance().setStyleSheet(themes.global_app_style())

        self.centralWidget().setStyleSheet(themes.main_area_style())
        self.header.setStyleSheet(themes.header_style())
        self.sidebar.setStyleSheet(themes.sidebar_style())
        self.pages.setStyleSheet(themes.page_background_style())
        self.version_label.setStyleSheet(themes.version_label_style())

        # Re-estilizar páginas
        self.dashboard_page.apply_theme()
        self.brokers_page.apply_theme()
        self.history_page.setStyleSheet(themes.history_page_style())
        self.logs_page.setStyleSheet(themes.logs_page_style())
        self.settings_page.apply_theme()

        # Re-estilizar centro de notificações
        if hasattr(self, "notification_center"):
            self.notification_center.apply_theme()

    # ── Signals ──
    def _connect_signals(self):
        self.tcp_message_handler.log_message_received.connect(self.logs_page.append_log)
        self.tcp_message_handler.log_message_received.connect(self._handle_tcp_messages)
        # Push periódico do EA (a cada ~2s): alimenta os cards sem polling.
        self.tcp_message_handler.account_update_received.connect(self.dashboard_page.update_account_info)
        self.tcp_message_handler.account_update_received.connect(self.brokers_page.update_account_info)
        # Atualizar indicadores quando status muda
        self.tcp_message_handler.trade_allowed_update_received.connect(
            lambda _: self._update_all_indicators())
        self.tcp_message_handler.connection_status_received.connect(
            lambda _: self._update_all_indicators())
        # Sincronizar dashboard quando broker conecta/desconecta via botão
        self.brokers_page.broker_status_changed.connect(self._on_broker_status_changed)
        if self.copytrade_manager:
            self.copytrade_manager.copy_trade_log.connect(self.logs_page.append_log)
            self.copytrade_manager.copy_trade_executed.connect(self.history_page.refresh)
            self.copytrade_manager.copy_trade_failed.connect(self.history_page.refresh)
            self.copytrade_manager.copy_trade_executed.connect(self.dashboard_page.refresh_stats)
            self.copytrade_manager.copy_trade_failed.connect(self.dashboard_page.refresh_stats)
            self.copytrade_manager.emergency_completed.connect(self._on_emergency_completed)
        # Alien trade detection
        self.tcp_message_handler.alien_trade_detected.connect(self._on_alien_trade_detected)
        # EA (.ex5) ausente — instrui o usuário a copiá-lo pra pasta correta
        self.broker_manager.ea_not_found.connect(self._on_ea_not_found)

    @Slot(str)
    def _handle_tcp_messages(self, message: str):
        status_changed = False
        for key in list(self.broker_status.keys()):
            if "REGISTER" in message and key in message and "UNREGISTER" not in message:
                self.broker_status[key] = True
                status_changed = True
                break
            elif ("CLIENT_UNREGISTERED" in message or "UNREGISTER" in message) and key in message:
                self.broker_status[key] = False
                status_changed = True
                break
        if status_changed:
            self.broker_status_updated.emit(self.broker_status, self.broker_modes)
            self.dashboard_page.refresh_brokers()
            self.brokers_page.refresh_brokers()

    @Slot(bool, str)
    def _on_emergency_completed(self, success: bool, message: str):
        """Feedback do fechamento de emergência: notificação + log."""
        level = NotificationLevel.INFO if success else NotificationLevel.ERROR
        title = "Emergencia concluida" if success else "Emergencia com avisos"
        self.notification_center.push(level, title, message)
        self.logs_page.append_log(message)

    def _on_broker_status_changed(self):
        """Clear stale status and refresh when broker connects/disconnects via GUI."""
        for key in list(self.broker_status.keys()):
            if not self.broker_manager.is_connected(key):
                self.broker_status[key] = False
                self.tcp_message_handler.clear_broker_status(key)
        self.dashboard_page.refresh_brokers()
        self.brokers_page.refresh_brokers()

    @Slot(dict)
    def _on_alien_trade_detected(self, data: dict):
        """Publica alerta não-modal no centro de notificações da barra superior.

        NÃO usar QMessageBox.warning() aqui — cria event loop aninhado
        (exec()) que deixa a janela inconsistente enquanto o modal fica aberto.
        """
        broker = data.get("broker_key", "?")
        symbol = data.get("symbol", "?")
        volume = data.get("volume", 0)
        deal_type = data.get("deal_type", "?")
        detail = f"{broker}: {deal_type} {symbol} {volume} lote(s)"
        self.notification_center.push(
            NotificationLevel.ERROR, "Alien Trade detectado", detail
        )

    @Slot(str)
    def _on_ea_not_found(self, expected_path: str):
        """O .ex5 do EA não foi localizado — avisa o usuário onde colocá-lo."""
        self.notification_center.push(
            NotificationLevel.ERROR,
            "EA nao encontrado",
            f"Copie o arquivo do EA (.ex5) para:\n{expected_path}\n\n"
            f"Ou defina o caminho do EA em Configuracoes.",
        )

    def _update_all_indicators(self):
        """Update indicators on both dashboard and brokers page."""
        self.dashboard_page.update_broker_indicators()
        self.brokers_page.update_broker_indicators()

    # ── System Monitor ──
    @Slot(dict)
    def _on_system_status(self, status):
        online = status.get("internet", "Offline")
        color = themes.internet_status_color(online)
        self.internet_label.setText(f"Internet: <span style='color:{color}'>{online}</span>")
        self.cpu_label.setText(status.get("cpu", "CPU: --%"))
        self.mem_label.setText(status.get("memory", "RAM: --%"))

    # ── Emergency ──
    def _on_emergency(self):
        reply = QMessageBox.warning(
            self, "EMERGENCIA",
            "Fechar TODAS as posicoes em TODAS as corretoras (Master + Slaves)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            if self.copytrade_manager and self.engine:
                # Submete ao loop do motor — emergency_close_all roda fora
                # da main thread e não bloqueia a GUI durante o fechamento.
                self.engine.submit(self.copytrade_manager.emergency_close_all())
                self.logs_page.append_log("EMERGENCIA: Fechando todas as posicoes...")
            else:
                QMessageBox.information(self, "Info", "CopyTradeManager nao inicializado.")

    # ── Window Events ──
    def showEvent(self, event):
        super().showEvent(event)
        logger.info("MainWindow exibida.")

    def closeEvent(self, event: QCloseEvent):
        """
        Confirma a saída (alertando sobre operações abertas) e então executa
        a sequência de shutdown ordenada (issue #111):
        1. Para timers e monitores da GUI (main thread).
        2. Desconecta todos os brokers — isso submete coroutines ao motor
           para fechar sockets do TcpRouter; aceitamos a latência e prosseguimos.
        3. Para MT5ProcessMonitor (thread separada).
        4. Submete coroutine de teardown do motor (TcpRouter.stop) e aguarda.
        5. Fecha CopyTradeManager (libera SQLite).
        6. Para EngineThread (cancela tasks pendentes, join na thread).
        7. event.accept() — Qt fecha a janela; app.exec() retorna.
        """
        # ── Confirmação de saída (com alerta de operações abertas) ──
        open_count = 0
        if self.copytrade_manager is not None:
            try:
                open_count = self.copytrade_manager.count_open_positions()
            except Exception as e:
                logger.warning(f"Falha ao contar operações abertas: {e}")

        if open_count > 0:
            msg1 = ("Deseja encerrar o EPCopyFlow 2.0?\n\n"
                    f"ATENCAO: ha {open_count} operacao(oes) aberta(s).")
        else:
            msg1 = "Deseja encerrar o EPCopyFlow 2.0?"
        reply1 = QMessageBox.question(
            self, "Encerrar EPCopyFlow 2.0", msg1,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply1 != QMessageBox.Yes:
            event.ignore()
            return

        if open_count > 0:
            reply2 = QMessageBox.warning(
                self, "Operacoes abertas",
                f"Ainda ha {open_count} operacao(oes) aberta(s).\n\n"
                "Recomendamos fechar as operacoes (ou usar o botao "
                "EMERGENCIA) antes de encerrar o programa.\n\n"
                "Encerrar mesmo assim?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply2 != QMessageBox.Yes:
                event.ignore()
                return

        logger.info("Fechando MainWindow — iniciando shutdown ordenado...")
        self.indicators_timer.stop()
        self.internet_monitor.stop()
        if hasattr(self, "notification_center"):
            self.notification_center.shutdown()

        # Desconectar todas as corretoras e fechar MT5 (síncrono nesta thread;
        # internamente submete os disconnect_broker_sockets ao motor).
        try:
            for key in list(self.broker_manager.get_connected_brokers()):
                try:
                    self.broker_manager.disconnect_broker(key)
                    logger.info(f"MT5 desconectado para {key} no fechamento.")
                except Exception as e:
                    logger.error(f"Erro ao desconectar {key} no fechamento: {e}")
        except Exception as e:
            logger.error(f"Erro ao obter corretoras conectadas no fechamento: {e}")

        # Para watchdog do MT5 (thread própria).
        if self.mt5_monitor is not None:
            try:
                self.mt5_monitor.stop()
            except Exception as e:
                logger.warning(f"Erro ao parar MT5ProcessMonitor: {e}")

        # Teardown do motor: parar TcpRouter dentro do loop dele.
        if self.engine is not None and self.tcp_router is not None:
            try:
                fut = self.engine.submit(self.tcp_router.stop())
                fut.result(timeout=5.0)
            except Exception as e:
                logger.warning(f"Erro/timeout ao parar TcpRouter: {e}")

        # Fechar CopyTradeManager (libera SQLite — importante no Windows).
        if self.copytrade_manager is not None:
            try:
                self.copytrade_manager.close()
            except Exception as e:
                logger.warning(f"Erro ao fechar CopyTradeManager: {e}")

        # Parar a thread do motor.
        if self.engine is not None:
            try:
                self.engine.stop(timeout=5.0)
            except Exception as e:
                logger.warning(f"Erro ao parar EngineThread: {e}")

        logger.info("Shutdown ordenado concluído.")
        event.accept()
