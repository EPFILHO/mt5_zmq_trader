# EPCopyFlow 2.0 - Versão 0.0.1 - Claude Code Parte 002
# gui/widgets/broker_card.py
# Card visual para exibir informações de uma corretora.

import logging
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
)
from PySide6.QtCore import Qt
from gui import themes

logger = logging.getLogger(__name__)

# Cores dos indicadores de status
_COLOR_GREEN = "#2ecc71"
_COLOR_RED = "#e74c3c"
_COLOR_GRAY = "#585b70"


class BrokerCard(QFrame):
    def __init__(self, broker_key, broker_data, is_connected=False,
                 show_connect_btn=False, on_connect=None, on_disconnect=None,
                 session_label=None, show_select_btn=False,
                 selected=False, on_select_toggled=None, parent=None):
        super().__init__(parent)
        self.broker_key = broker_key
        self.broker_data = broker_data
        self.is_connected = is_connected
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self.session_label = session_label
        self._show_select_btn = show_select_btn
        self._selected = selected
        self._on_select_toggled = on_select_toggled

        role = broker_data.get("role", "slave")
        is_master = role == "master"

        border_color, role_color, role_bg, status_color = themes.broker_card_dynamic_colors(
            is_master, is_connected
        )

        style = themes.broker_card_style(border_color, role_color, role_bg, status_color)
        self.setStyleSheet(style)
        self.setProperty("class", "broker-card")
        # Largura fixa 220px → cabem 5 cards em janela 1400 (sidebar 200 + padding 48).
        # Altura natural pelo conteúdo — todos os cards mostram os mesmos campos.
        self.setFixedWidth(220)

        self._init_ui(broker_key, broker_data, role, is_connected, show_connect_btn)

    def _init_ui(self, key, data, role, is_connected, show_connect_btn):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        # Row 1: Session badge + Name + Role badge
        top_row = QHBoxLayout()
        if self.session_label:
            is_master = role == "master"
            badge_bg = "#ff8c00" if is_master else "#3478f6"
            badge = QLabel(self.session_label)
            badge.setAlignment(Qt.AlignCenter)
            badge.setFixedSize(22, 22)
            badge.setStyleSheet(
                f"background-color: {badge_bg}; color: white; "
                f"border-radius: 11px; font-weight: bold; font-size: 12px;"
            )
            top_row.addWidget(badge)
        broker_name = data.get("broker_name", key.split("-")[0])
        title = QLabel(f"{broker_name}")
        title.setProperty("class", "card-title")
        top_row.addWidget(title)
        top_row.addStretch()

        role_label = QLabel(role.upper())
        role_label.setProperty("class", "card-role")
        top_row.addWidget(role_label)
        layout.addLayout(top_row)

        # Row 2: Key + status
        row2 = QHBoxLayout()
        key_label = QLabel(key)
        key_label.setProperty("class", "card-info")
        row2.addWidget(key_label)
        row2.addStretch()

        self.status_label = QLabel("Conectado" if is_connected else "Desconectado")
        self.status_label.setProperty("class", "card-status")
        row2.addWidget(self.status_label)
        layout.addLayout(row2)

        # Row 3: Status indicators (MT5, EA, BRK, ALG)
        indicators_row = QHBoxLayout()
        indicators_row.setSpacing(12)
        self._indicators = {}
        for name in ("MT5", "EA", "BRK", "ALG"):
            dot = QLabel("\u25CF")  # ● character
            dot.setStyleSheet(
                f"color: {_COLOR_GRAY}; font-size: 18px; background-color: transparent;"
            )
            dot.setAlignment(Qt.AlignCenter)
            lbl = QLabel(name)
            lbl.setProperty("class", "card-info")
            lbl.setStyleSheet("font-size: 11px;")
            pair = QHBoxLayout()
            pair.setSpacing(2)
            pair.addWidget(dot)
            pair.addWidget(lbl)
            indicators_row.addLayout(pair)
            self._indicators[name] = dot
        indicators_row.addStretch()
        layout.addLayout(indicators_row)

        # Row 4: Client + Lot multiplier
        row4 = QHBoxLayout()
        client = data.get("client", data.get("name", "-"))
        client_label = QLabel(f"Cliente: {client}")
        client_label.setProperty("class", "card-info")
        row4.addWidget(client_label)
        row4.addStretch()

        mult = data.get("lot_multiplier", 1.0)
        mult_label = QLabel(f"Mult: {mult:.2f}x")
        mult_label.setProperty("class", "card-info")
        row4.addWidget(mult_label)
        layout.addLayout(row4)

        # Row 5: Balance + Positions count
        row5 = QHBoxLayout()
        self.balance_label = QLabel("Saldo: --")
        self.balance_label.setProperty("class", "card-info")
        row5.addWidget(self.balance_label)
        row5.addStretch()

        self.positions_label = QLabel("Posicoes: --")
        self.positions_label.setProperty("class", "card-info")
        row5.addWidget(self.positions_label)
        layout.addLayout(row5)

        # Row 6: P/L atual (operação aberta — POSITION_PROFIT acumulado)
        self.profit_label = QLabel("P/L: --")
        self.profit_label.setProperty("class", "card-profit-positive")
        layout.addWidget(self.profit_label)

        # Row 7: P/L do dia (deals fechados desde meia-noite)
        self.daily_profit_label = QLabel("P/L Dia: --")
        self.daily_profit_label.setProperty("class", "card-profit-positive")
        layout.addWidget(self.daily_profit_label)

        # Connect/Disconnect button (only on brokers page)
        if show_connect_btn:
            btn_row = QHBoxLayout()
            btn_row.addStretch()
            # Toggle "Selecionar" — só faz sentido em slave desconectado
            # (alvo do botão "Conectar Selecionados" da página).
            if self._show_select_btn and not is_connected:
                self.select_btn = QPushButton(
                    "Selecionado" if self._selected else "Selecionar"
                )
                self.select_btn.setCheckable(True)
                self.select_btn.setChecked(self._selected)
                self.select_btn.setStyleSheet(
                    "QPushButton { background-color: #45475a; color: #cdd6f4;"
                    " border: none; border-radius: 4px; padding: 4px 10px; }"
                    "QPushButton:hover { background-color: #585b70; }"
                    f"QPushButton:checked {{ background-color: {_COLOR_GREEN};"
                    " color: white; }"
                )
                self.select_btn.toggled.connect(self._handle_select_toggled)
                btn_row.addWidget(self.select_btn)
            if is_connected:
                btn = QPushButton("Desconectar")
                btn.setProperty("class", "card-disconnect")
                if self._on_disconnect:
                    btn.clicked.connect(lambda _checked=False: self._on_disconnect())
            else:
                btn = QPushButton("Conectar")
                btn.setProperty("class", "card-connect")
                if self._on_connect:
                    btn.clicked.connect(lambda _checked=False: self._on_connect())
            btn_row.addWidget(btn)
            layout.addLayout(btn_row)

    def _handle_select_toggled(self, checked):
        self.select_btn.setText("Selecionado" if checked else "Selecionar")
        if self._on_select_toggled:
            self._on_select_toggled(self.broker_key, checked)

    def update_status_indicators(self, mt5=None, ea=None, brk=None, alg=None):
        """Update the 4 status indicator dots.

        Each parameter accepts: True (green), False (red), None (gray).
        """
        mapping = {"MT5": mt5, "EA": ea, "BRK": brk, "ALG": alg}
        for name, value in mapping.items():
            if name in self._indicators:
                if value is True:
                    color = _COLOR_GREEN
                elif value is False:
                    color = _COLOR_RED
                else:
                    color = _COLOR_GRAY
                self._indicators[name].setStyleSheet(
                    f"color: {color}; font-size: 18px; background-color: transparent;"
                )

    def update_account_info(self, data):
        """Atualiza balance / positions_count / profit / daily_profit a partir
        do STREAM ACCOUNT_UPDATE do EA (push periódico, ~2s). Esta é a única
        fonte de atualização dos campos do card em runtime."""
        balance = data.get("balance", 0.0) or 0.0
        self.balance_label.setText(f"Saldo: {balance:,.2f}")

        positions_count = data.get("positions_count", 0) or 0
        self.positions_label.setText(f"Posicoes: {positions_count}")

        profit = data.get("profit", 0.0) or 0.0
        self._set_pnl_label(self.profit_label, "P/L", profit)

        daily = data.get("daily_profit", 0.0) or 0.0
        self._set_pnl_label(self.daily_profit_label, "P/L Dia", daily)

    def _set_pnl_label(self, label, prefix_text, value):
        sign = "+" if value >= 0 else ""
        label.setText(f"{prefix_text}: {sign}{value:,.2f}")
        prop = "card-profit-positive" if value >= 0 else "card-profit-negative"
        label.setProperty("class", prop)
        label.style().unpolish(label)
        label.style().polish(label)
