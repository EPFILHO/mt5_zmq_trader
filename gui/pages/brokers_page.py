# EPCopyFlow 2.0 - Versão 0.0.1 - Claude Code Parte 000
# gui/pages/brokers_page.py
# Página de gerenciamento de corretoras: cadastro, conexão/desconexão.

import logging
import asyncio
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QMessageBox
)
from PySide6.QtCore import Slot, Qt, Signal
from gui.brokers_dialog import BrokersDialog
from gui.widgets.broker_card import BrokerCard
from gui.widgets.flow_layout import FlowLayout
from gui import themes

logger = logging.getLogger(__name__)


class BrokersPage(QWidget):
    broker_status_changed = Signal()

    def __init__(self, config, broker_manager, tcp_router, mt5_monitor,
                 tcp_message_handler=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.broker_manager = broker_manager
        self.tcp_router = tcp_router
        self.mt5_monitor = mt5_monitor
        self.tcp_message_handler = tcp_message_handler
        self._broker_status = {}
        self.broker_cards = {}
        # Keys de slaves marcados via checkbox. Vive na página (não no card)
        # porque os cards são destruídos/recriados a cada refresh_brokers().
        self._selected_keys = set()
        # Debounce de refresh_brokers: cada REGISTER/UNREGISTER de EA dispara
        # refresh; com 9 brokers conectando, isso vira ~18 refreshes em 1-2s.
        # Coalesce em 50ms evita janelas Qt piscando durante destroy/recreate.
        self._refresh_brokers_pending = False
        self.setStyleSheet(themes.brokers_page_style())
        self._init_ui()
        self.refresh_brokers()

    def set_broker_status(self, broker_status):
        """Reference to main_window.broker_status dict."""
        self._broker_status = broker_status

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(16)

        # Title + action buttons
        header = QHBoxLayout()
        title = QLabel("Corretoras")
        title.setProperty("class", "page-title")
        header.addWidget(title)
        header.addStretch()

        self.cadastro_btn = QPushButton("Cadastrar / Editar")
        self.cadastro_btn.setProperty("class", "action-btn")
        self.cadastro_btn.clicked.connect(self._open_broker_dialog)
        header.addWidget(self.cadastro_btn)

        # Atualiza o EA (.ex5) em todas as instâncias cadastradas. Útil depois
        # de recompilar no MetaEditor — evita ter que copiar à mão.
        self.update_ea_btn = QPushButton("Atualizar EA")
        self.update_ea_btn.setProperty("class", "action-btn")
        self.update_ea_btn.clicked.connect(self._update_ea)
        header.addWidget(self.update_ea_btn)

        self.connect_all_btn = QPushButton("Conectar Todas")
        self.connect_all_btn.setProperty("class", "connect-btn")
        self.connect_all_btn.clicked.connect(self._connect_all)
        header.addWidget(self.connect_all_btn)

        self.connect_selected_btn = QPushButton("Conectar Selecionados")
        self.connect_selected_btn.setProperty("class", "connect-btn")
        self.connect_selected_btn.clicked.connect(self._connect_selected)
        header.addWidget(self.connect_selected_btn)

        self.disconnect_all_btn = QPushButton("Desconectar Todas")
        self.disconnect_all_btn.setProperty("class", "disconnect-btn")
        self.disconnect_all_btn.clicked.connect(self._disconnect_all)
        header.addWidget(self.disconnect_all_btn)

        layout.addLayout(header)

        # Master section
        master_label = QLabel("Master")
        master_label.setProperty("class", "section-title")
        layout.addWidget(master_label)

        self.master_area = QHBoxLayout()
        self.master_placeholder = QLabel("Nenhum Master configurado")
        self.master_placeholder.setStyleSheet(themes.dashboard_placeholder_style())
        self.master_area.addWidget(self.master_placeholder)
        self.master_area.addStretch()
        layout.addLayout(self.master_area)

        # Slaves section
        slaves_label = QLabel("Slaves")
        slaves_label.setProperty("class", "section-title")
        layout.addWidget(slaves_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(themes.scroll_area_style())
        scroll_widget = QWidget()
        scroll_widget.setStyleSheet(themes.scroll_widget_style())
        self.slaves_grid = FlowLayout(scroll_widget, margin=0, hspacing=12, vspacing=12)
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, 1)

    def apply_theme(self):
        self.setStyleSheet(themes.brokers_page_style())
        self.refresh_brokers()

    def refresh_brokers(self):
        """Coalesce múltiplos refreshes em ~50ms — evita destroy/recreate em
        rajada quando vários EAs registram em sequência."""
        if self._refresh_brokers_pending:
            return
        self._refresh_brokers_pending = True
        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, self._do_refresh_brokers)

    def _do_refresh_brokers(self):
        self._refresh_brokers_pending = False

        # hide() antes de setParent(None) evita janelas Qt piscando.
        for card in self.broker_cards.values():
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.broker_cards.clear()

        # Remove master placeholder if present
        if self.master_placeholder.parent():
            self.master_placeholder.hide()
            self.master_placeholder.setParent(None)

        # Clear slaves grid
        while self.slaves_grid.count():
            item = self.slaves_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)

        brokers = self.broker_manager.get_brokers()
        connected = self.broker_manager.get_connected_brokers()
        master_key = self.broker_manager.get_master_broker()
        slave_keys = sorted([k for k in brokers if k != master_key])

        # parent=self garante que o card nunca seja top-level antes de ser
        # re-parented pelo addWidget — evita "janelinhas piscando".
        if master_key:
            card = BrokerCard(
                master_key, brokers[master_key],
                is_connected=(master_key in connected),
                show_connect_btn=True,
                on_connect=lambda k=master_key: self._connect_broker(k),
                on_disconnect=lambda k=master_key: self._disconnect_broker(k),
                session_label=self.broker_manager.get_session_label(master_key),
                parent=self,
            )
            self.broker_cards[master_key] = card
            self.master_area.insertWidget(0, card)
        else:
            self.master_placeholder.show()
            self.master_area.insertWidget(0, self.master_placeholder)

        # Descarta da seleção keys que não existem mais (broker removido).
        self._selected_keys &= set(slave_keys)

        # Slave cards in flow layout (colunas adaptam à largura disponível)
        for key in slave_keys:
            card = BrokerCard(
                key, brokers[key],
                is_connected=(key in connected),
                show_connect_btn=True,
                on_connect=lambda k=key: self._connect_broker(k),
                on_disconnect=lambda k=key: self._disconnect_broker(k),
                session_label=self.broker_manager.get_session_label(key),
                show_select_btn=True,
                selected=(key in self._selected_keys),
                on_select_toggled=self._on_card_select_toggled,
                parent=self,
            )
            self.broker_cards[key] = card
            self.slaves_grid.addWidget(card)

        self.update_broker_indicators()

    @Slot(dict)
    def update_account_info(self, data):
        """Push periódico do EA (STREAM ACCOUNT_UPDATE) — atualiza balance,
        positions_count e P/L do card sem precisar pedir."""
        broker_key = data.get("broker_key")
        if broker_key and broker_key in self.broker_cards:
            self.broker_cards[broker_key].update_account_info(data)

    @Slot()
    def update_broker_indicators(self):
        """Update all 4 status indicators (MT5, EA, BRK, ALG) on every broker card."""
        trade_allowed = {}
        connection_status = {}
        if self.tcp_message_handler:
            trade_allowed = self.tcp_message_handler.get_trade_allowed_states()
            connection_status = self.tcp_message_handler.get_connection_status_states()

        for key, card in self.broker_cards.items():
            mt5_running = self.mt5_monitor.is_running(key) if self.mt5_monitor else False

            if not mt5_running:
                card.update_status_indicators(mt5=None, ea=None, brk=None, alg=None)
                continue

            ea_registered = self._broker_status.get(key, False)

            if not ea_registered:
                card.update_status_indicators(mt5=True, ea=False, brk=None, alg=None)
                continue

            brk = connection_status.get(key)
            alg = trade_allowed.get(key)

            card.update_status_indicators(
                mt5=True,
                ea=True,
                brk=brk,
                alg=alg,
            )

    def _open_broker_dialog(self):
        dialog = BrokersDialog(self.config, self.broker_manager, parent=self)
        dialog.brokers_updated.connect(self.refresh_brokers)
        dialog.exec()

    def _update_ea(self):
        """Copia o .ex5 do MT5 base pra cada instância cadastrada."""
        if not self.broker_manager.brokers:
            QMessageBox.information(
                self,
                "Atualizar EA",
                "Nenhuma corretora cadastrada.\n\n"
                "Cadastre ao menos uma corretora antes de atualizar o EA."
            )
            return
        sucessos, falhas = self.broker_manager.update_ea_in_all_instances()
        if sucessos == 0 and falhas > 0:
            expected = (
                f"{self.broker_manager.base_mt5_path}\\MQL5\\Experts\\EPCopyFlow2.0_EA.ex5"
            )
            QMessageBox.warning(
                self,
                "Atualizar EA",
                f"Nenhuma instância recebeu o EA.\n\n"
                f"O arquivo esperado é:\n{expected}\n\n"
                f"Você pode:\n"
                f"• abrir o .mq5 no MetaEditor do MT5 base e compilar (F7)\n"
                f"• ou definir o caminho do .ex5 em Configurações → CopyTrade"
            )
            return

        msg = f"EA copiado para {sucessos} instância(s)."
        if falhas > 0:
            msg += f"\n{falhas} falha(s) — ver logs."
        msg += (
            "\n\nAs instâncias MT5 já em execução ainda usam o .ex5 anterior em "
            "memória. Para aplicar a versão nova:\n"
            "• remova o EA do gráfico (botão direito → Remove)\n"
            "• arraste o EA do Navigator de volta no gráfico"
        )
        QMessageBox.information(self, "Atualizar EA", msg)

    def _connect_broker(self, key):
        try:
            self.broker_manager.connect_broker(key)
            logger.info(f"Corretora {key} conectada.")
            self.refresh_brokers()
            self.broker_status_changed.emit()
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Erro ao conectar {key}: {e}")
            logger.error(f"Erro ao conectar {key}: {e}")

    def _disconnect_broker(self, key):
        try:
            self.broker_manager.disconnect_broker(key)
            logger.info(f"Corretora {key} desconectada.")
            self.refresh_brokers()
            self.broker_status_changed.emit()
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Erro ao desconectar {key}: {e}")
            logger.error(f"Erro ao desconectar {key}: {e}")

    def _connect_all(self):
        brokers = self.broker_manager.get_brokers()
        connected = self.broker_manager.get_connected_brokers()
        for key in brokers:
            if key not in connected:
                try:
                    self.broker_manager.connect_broker(key)
                except Exception as e:
                    logger.error(f"Erro ao conectar {key}: {e}")
        self.refresh_brokers()
        self.broker_status_changed.emit()

    def _on_card_select_toggled(self, key, checked):
        if checked:
            self._selected_keys.add(key)
        else:
            self._selected_keys.discard(key)

    def _connect_selected(self):
        if not self._selected_keys:
            QMessageBox.information(
                self, "Conectar Selecionados",
                "Nenhum slave selecionado.\n\n"
                "Marque a caixa de seleção nos cards que deseja conectar."
            )
            return
        connected = self.broker_manager.get_connected_brokers()
        brokers = self.broker_manager.get_brokers()
        for key in list(self._selected_keys):
            if key in brokers and key not in connected:
                try:
                    self.broker_manager.connect_broker(key)
                except Exception as e:
                    logger.error(f"Erro ao conectar {key}: {e}")
        self._selected_keys.clear()
        self.refresh_brokers()
        self.broker_status_changed.emit()

    def _disconnect_all(self):
        connected = list(self.broker_manager.get_connected_brokers())
        for key in connected:
            try:
                self.broker_manager.disconnect_broker(key)
            except Exception as e:
                logger.error(f"Erro ao desconectar {key}: {e}")
        self.refresh_brokers()
        self.broker_status_changed.emit()
