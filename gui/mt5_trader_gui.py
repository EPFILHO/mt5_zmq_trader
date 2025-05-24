# gui/mt5_trader_gui.py
# Versão 1.0.9.e - Envio 3
# Ajustes:
# - Modificado send_data_command para usar send_command_to_broker com use_data_port=True, removendo port_type.
# - Mantidas todas as correções do Envio 1 (1.0.9.e):
#   - Validações para GET_INDICATOR_MA, GET_OHLC, GET_TICK.
#   - Sinais indicator_ma_received, ohlc_received, tick_received.
# - Mantidas todas as funcionalidades do envio 7 (1.0.9.a):
#   - Aba "Indicadores" com campos e botões.
#   - Funcionalidades de Copy Trade, HISTORY_DATA, TRADE_*, etc.
# - Versão alinhada com ZmqTraderBridge 1.0.9.e e ZmqRouter 1.0.9.a (envio 5).

import sys
import json
import time
import logging
import asyncio
from PySide6.QtWidgets import (
    QApplication, QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QPushButton, QTextEdit, QTabWidget, QFormLayout,
    QLineEdit, QCheckBox, QLabel, QGridLayout
)
from PySide6.QtCore import Slot

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

class MT5TraderGui(QDialog):
    def __init__(self, config, broker_manager, zmq_router, zmq_message_handler, main_window, parent=None):
        super().__init__(parent)
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.zmq_message_handler = zmq_message_handler
        self.main_window = main_window
        self.copy_trade_enabled = False
        self.setWindowTitle("MT5Trader GUI")
        self.setGeometry(100, 100, 600, 450)
        self.setMinimumWidth(600)
        self.setup_ui()
        self._connect_signals()
        self._populate_brokers()
        self._update_buttons()
        logger.info("MT5TraderGui inicializado.")

    def setup_ui(self):
        layout = QVBoxLayout(self)

        self.broker_combo = QComboBox()
        layout.addWidget(QLabel("Selecione a Corretora:"))
        layout.addWidget(self.broker_combo)

        self.copy_trade_checkbox = QCheckBox("Ativar Copy Trade (replicar ordens para outra conta)")
        self.copy_trade_checkbox.stateChanged.connect(self.toggle_copy_trade)
        layout.addWidget(self.copy_trade_checkbox)

        self.monitor_btn = QPushButton("Parar Monitoramento")
        self.monitor_btn.setMaximumWidth(150)
        self.monitor_btn.setStyleSheet("padding: 5px;")
        self.monitor_btn.setEnabled(False)
        layout.addWidget(self.monitor_btn)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        admin_tab = QWidget()
        admin_layout = QVBoxLayout(admin_tab)
        admin_buttons_layout = QGridLayout()

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

        indicators_tab = QWidget()
        indicators_layout = QVBoxLayout(indicators_tab)
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

        indicators_buttons_layout = QHBoxLayout()
        indicator_commands = ["GET_INDICATOR_MA", "GET_OHLC", "GET_TICK"]
        self.indicator_buttons = {}
        for cmd in indicator_commands:
            btn = QPushButton(cmd)
            btn.setMaximumWidth(150)
            btn.setStyleSheet("padding: 5px;")
            btn.clicked.connect(lambda checked, c=cmd: self.send_admin_command(c))
            self.indicator_buttons[cmd] = btn
            indicators_buttons_layout.addWidget(btn)

        indicators_layout.addLayout(indicators_form_layout)
        indicators_layout.addLayout(indicators_buttons_layout)
        tabs.addTab(indicators_tab, "Indicadores")

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(300)
        layout.addWidget(QLabel("Log de Comandos e Respostas:"))
        layout.addWidget(self.log_area)

        stop_btn = QPushButton("Fechar")
        stop_btn.setMaximumWidth(150)
        stop_btn.setStyleSheet("padding: 5px;")
        stop_btn.clicked.connect(self.close)
        layout.addWidget(stop_btn)

    def _connect_signals(self):
        self.broker_combo.currentIndexChanged.connect(self._update_buttons)
        self.zmq_message_handler.log_message_received.connect(self.update_log)
        self.main_window.broker_status_updated.connect(self._update_buttons)
        self.main_window.broker_connected.connect(self._select_broker)
        self.zmq_message_handler.positions_received.connect(self._update_positions)
        self.zmq_message_handler.orders_received.connect(self._update_orders)
        self.zmq_message_handler.history_data_received.connect(self._update_history_data)
        self.zmq_message_handler.history_trades_received.connect(self._update_history_trades)
        self.zmq_message_handler.trade_response_received.connect(self._update_trade_response)
        self.zmq_message_handler.indicator_ma_received.connect(self._update_indicator_ma)
        self.zmq_message_handler.ohlc_received.connect(self._update_ohlc)
        self.zmq_message_handler.tick_received.connect(self._update_tick)
        logger.debug("Sinais conectados no MT5TraderGui.")

    def _populate_brokers(self):
        self.broker_combo.clear()
        connected_brokers = self.broker_manager.get_connected_brokers()
        for key in sorted(connected_brokers):
            self.broker_combo.addItem(key)
        self._update_buttons()
        logger.info(
            f"Lista de corretoras atualizada na QComboBox: {[self.broker_combo.itemText(i) for i in range(self.broker_combo.count())]}")

    @Slot(str)
    def _select_broker(self, broker_key: str):
        index = self.broker_combo.findText(broker_key)
        if index >= 0:
            self.broker_combo.setCurrentIndex(index)
            logger.info(f"Corretora {broker_key} selecionada automaticamente na QComboBox.")
        else:
            logger.debug(f"Corretora {broker_key} não encontrada na QComboBox.")

    def _update_buttons(self):
        selected_key = self.broker_combo.currentText()
        is_registered = bool(
            selected_key and selected_key in self.main_window.broker_status and self.main_window.broker_status[
                selected_key])

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

        logger.debug(f"Botões atualizados para corretora {selected_key}. Registrada: {is_registered}.")

    def toggle_copy_trade(self, state):
        self.copy_trade_enabled = state == 2
        log_msg = f"Copy Trade {'ativado' if self.copy_trade_enabled else 'desativado'}"
        logger.info(log_msg)
        self.update_log(log_msg)

    async def send_command(self, broker_key, command, payload, command_type='admin'):
        request_id = f"{command.lower()}_{broker_key}_{int(time.time())}"
        try:
            response = await self.zmq_router.send_command_to_broker(broker_key, command, payload, request_id)
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
        request_id = f"{command.lower()}_{broker_key}_{int(time.time())}"
        try:
            response = await self.zmq_router.send_command_to_broker(
                broker_key, command, payload, request_id, use_data_port=True
            )
            if isinstance(response, dict):
                if response.get("status") == "ERROR":
                    log_msg = f"Erro: {response.get('message', 'Falha desconhecida')}"
                    self.update_log(log_msg)
                    logger.error(f"Falha ao enviar {command} para {broker_key} via DataPort: {response.get('message')}")
                else:
                    logger.info(f"Comando {command} enviado para {broker_key} via DataPort: {response}")
            else:
                log_msg = f"Erro: Resposta inválida para {command} via DataPort."
                self.update_log(log_msg)
                logger.error(f"Resposta inválida para {command} de {broker_key} via DataPort: {response}")
        except asyncio.TimeoutError:
            log_msg = f"Erro: Timeout ao aguardar resposta para {command} via DataPort."
            self.update_log(log_msg)
            logger.error(f"Timeout ao enviar {command} para {broker_key} via DataPort")
        except Exception as e:
            log_msg = f"Erro ao enviar comando via DataPort: {str(e)}"
            self.update_log(log_msg)
            logger.error(f"Exceção ao enviar {command} para {broker_key} via DataPort: {str(e)}")

    async def send_copy_trade(self, primary_broker, command, message):
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

    def send_admin_command(self, command):
        broker_key = self.broker_combo.currentText()
        if not broker_key:
            self.update_log("Erro: Nenhuma corretora selecionada.")
            logger.warning("Nenhuma corretora selecionada ao tentar enviar comando.")
            return
        payload = {}
        try:
            if command == "HISTORY_DATA":
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
            elif command == "PING":
                payload = {"timestamp": int(time.time())}
                asyncio.create_task(self.send_command(broker_key, command, payload, 'admin'))
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
            else:
                asyncio.create_task(self.send_command(broker_key, command, payload, 'admin'))
        except ValueError as e:
            self.update_log(f"❌ Erro nos parâmetros: {str(e)}")

    def send_trade_command(self, command):
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
                "TRADE_POSITION_CLOSE_SYMBOL"
            ]:
                message = {"broker_key": broker_key, "command": command, "payload": payload,
                           "request_id": f"{command.lower()}_{broker_key}_{int(time.time())}"}
                asyncio.create_task(self.send_copy_trade(broker_key, command, message))
        except ValueError as e:
            self.update_log(f"❌ Erro nos parâmetros: {str(e)}")

    @Slot(str)
    def update_log(self, message):
        if "TICK" not in message:
            self.log_area.append(message)
            lines = self.log_area.toPlainText().split('\n')
            if len(lines) > 1000:
                self.log_area.setText('\n'.join(lines[-1000:]))

    @Slot(dict)
    def _update_positions(self, positions):
        text = f"Posições: {json.dumps(positions, indent=2)}"
        self.log_area.append(text)
        logger.debug(f"Posições atualizadas: {text}")

    @Slot(dict)
    def _update_orders(self, orders):
        text = f"Ordens: {json.dumps(orders, indent=2)}"
        self.log_area.append(text)
        logger.debug(f"Ordens atualizadas: {text}")

    @Slot(dict)
    def _update_history_data(self, history_data):
        text = f"Histórico de Dados: {json.dumps(history_data, indent=2)}"
        self.log_area.append(text)
        logger.debug(f"Histórico de dados atualizado: {text}")

    @Slot(dict)
    def _update_history_trades(self, history_trades):
        text = f"Histórico de Trades: {json.dumps(history_trades, indent=2)}"
        self.log_area.append(text)
        logger.debug(f"Histórico de trades atualizado: {text}")

    @Slot(dict)
    def _update_trade_response(self, trade_response):
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
        status = "✅ Sucesso" if 'ma_value' in data else "❌ Erro"
        text = f"Média Móvel ({data.get('broker_key')}): {json.dumps(data, indent=2)}\nStatus: {status}"
        self.log_area.append(text)
        logger.debug(f"Média Móvel atualizada: {text}")

    @Slot(dict)
    def _update_ohlc(self, data):
        status = "✅ Sucesso" if 'ohlc' in data and data['ohlc'] else "❌ Erro"
        text = f"OHLC ({data.get('broker_key')}): {json.dumps(data, indent=2)}\nStatus: {status}"
        self.log_area.append(text)
        logger.debug(f"OHLC atualizado: {text}")

    @Slot(dict)
    def _update_tick(self, data):
        status = "✅ Sucesso" if 'tick' in data and data['tick'] else "❌ Erro"
        text = f"Tick ({data.get('broker_key')}): {json.dumps(data, indent=2)}\nStatus: {status}"
        self.log_area.append(text)
        logger.debug(f"Tick atualizado: {text}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MT5TraderGui(None, None, None, None, None)
    window.show()
    sys.exit(app.exec())

# ------------ término do arquivo mt5_trader_gui.py ------------
# Versão 1.0.9.e - Envio 3