# Arquivo: core/zmq_message_handler.py
# Versão: 1.0.9.k - Envio 4
# Objetivo: Manipular e rotear mensagens ZMQ recebidas do Expert Advisor (EA) para a aplicação Python.
# Ajustes:
# - Código reorganizado em blocos modulares para melhor organização e manutenção.
# - Comentários detalhados adicionados para cada bloco e função.
# - [FIX 1] Adicionado sinal trade_event_received e tratamento para mensagens STREAM/TRADE_EVENT.
# - [FIX 2] Corrigida a extração dos dados de 'request' e 'result' para o evento TRADE_EVENT,
#           considerando a estrutura real da mensagem recebida do EA (dados sob a chave vazia '').

# Bloco 1 - Importações e Configuração Inicial
# Objetivo: Importar bibliotecas necessárias e configurar o logging para depuração e monitoramento.
# Este bloco define as dependências do sistema e o formato de logs para rastrear eventos e erros.
import logging
import time
import asyncio
from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)

# Buffer global para armazenar o último estado do trade_allowed por corretora.
# Usado para que a GUI de status possa consultar o estado de algotrading de cada corretora.
trade_allowed_states = {}


# Bloco 2 - Definição da Classe ZmqMessageHandler
# Objetivo: Definir a classe principal para manipulação de mensagens ZMQ, seus sinais e inicializar atributos.
# Esta classe atua como um "tradutor" de mensagens do EA para a aplicação Python, emitindo sinais
# específicos para diferentes tipos de dados e eventos.
class ZmqMessageHandler(QObject):
    # Sinais emitidos para a interface do usuário ou outros componentes da aplicação.
    log_message_received = Signal(str)
    ping_button_state_changed = Signal(bool)
    broker_info_received = Signal(dict)
    account_info_received = Signal(dict)
    account_balance_received = Signal(dict)
    account_leverage_received = Signal(dict)
    account_flags_received = Signal(dict)
    account_margin_received = Signal(dict)
    account_state_received = Signal(dict)
    time_server_received = Signal(dict)
    status_info_received = Signal(dict)
    positions_received = Signal(dict)
    orders_received = Signal(dict)
    history_data_received = Signal(dict)
    history_trades_received = Signal(dict)
    trade_response_received = Signal(dict)
    indicator_ma_received = Signal(dict)
    ohlc_received = Signal(dict)
    tick_received = Signal(dict)
    stream_ohlc_received = Signal(dict)
    stream_ohlc_indicators_received = Signal(dict)
    trade_allowed_update_received = Signal(dict)
    trade_event_received = Signal(dict)  # [FIX 1] Novo sinal para eventos de trade (operações de mercado)

    def __init__(self, config, zmq_router, parent=None):
        """
        Inicializa o manipulador de mensagens ZMQ.

        Args:
            config: Instância do gerenciador de configurações.
            zmq_router: Instância do roteador ZMQ para comunicação.
            parent: Widget pai (opcional, padrão None).
        """
        super().__init__(parent)
        self.config = config
        self.zmq_router = zmq_router
        self.main_window = parent  # Referência à janela principal para acesso a outros componentes.
        self.heartbeat_active = {}  # Dicionário para rastrear o status do heartbeat por corretora.

    # Bloco 3 - Manipulação de Mensagens ZMQ (`handle_zmq_message`)
    # Objetivo: Receber, decodificar e rotear mensagens ZMQ para os sinais e componentes apropriados.
    # Este é o método central que processa todas as mensagens recebidas do Expert Advisor (EA).
    @Slot(bytes, object)
    async def handle_zmq_message(self, client_id_bytes: bytes, message: dict):
        """
        Processa uma mensagem ZMQ recebida do Expert Advisor.

        Args:
            client_id_bytes (bytes): O ID ZMQ do cliente (EA) que enviou a mensagem.
            message (dict): O dicionário da mensagem JSON recebida.
        """
        global trade_allowed_states  # Acessa o buffer global de estados de trade_allowed.

        # Identifica a chave da corretora associada ao client_id_bytes.
        client_id_hex = client_id_bytes.hex()
        identified_broker_key = None
        for key, zid in self.zmq_router._clients.items():
            if zid == client_id_bytes:
                identified_broker_key = key
                break
        # Se não encontrado pelo ID ZMQ, tenta obter do próprio corpo da mensagem.
        if not identified_broker_key:
            identified_broker_key = message.get("broker_key")

        log_prefix = f"ZMQ RX [{identified_broker_key or client_id_hex}]:"

        # Loga a mensagem, exceto para TICKs que podem ser muito frequentes.
        if message.get("event") != "TICK":
            log_message = f"{log_prefix} {message}"
            self.log_message_received.emit(log_message)
            logger.debug(log_message)
        else:
            logger.debug(f"{log_prefix} {message}")

        msg_type = message.get("type")
        event = message.get("event")
        status = message.get("status")

        # Sub-bloco 3.1 - Eventos de Sistema (REGISTER, CLIENT_UNREGISTERED)
        if msg_type == "SYSTEM" and event == "REGISTER":
            broker_key_from_msg = message.get("broker_key")
            if broker_key_from_msg:
                self.log_message_received.emit(f"INFO: Corretora {broker_key_from_msg} registrada.")
                logger.info(f"Corretora {broker_key_from_msg} registrada.")
                self.ping_button_state_changed.emit(True)  # Sinaliza que o botão PING pode ser habilitado.
                self.heartbeat_active[broker_key_from_msg] = True
            else:
                logger.warning(f"Registro sem broker_key de {client_id_hex}")

        elif msg_type == "INTERNAL" and event == "CLIENT_UNREGISTERED":
            unregistered_key = message.get("broker_key")
            if unregistered_key:
                self.log_message_received.emit(f"INFO: Corretora {unregistered_key} desconectada.")
                logger.info(f"Corretora {unregistered_key} desconectada.")
                self.ping_button_state_changed.emit(False)  # Sinaliza que o botão PING deve ser desabilitado.
                if unregistered_key in self.heartbeat_active:
                    del self.heartbeat_active[unregistered_key]
                if unregistered_key in trade_allowed_states:  # Remover do buffer ao desregistrar.
                    del trade_allowed_states[unregistered_key]
            else:
                logger.warning(f"Desregistro sem broker_key de {client_id_hex}")

        # Sub-bloco 3.2 - Eventos de Stream (HEARTBEAT, OHLC, Indicadores, Trade Allowed, Trade Event)
        elif msg_type == "EVENT" and event == "HEARTBEAT":
            broker_key_hb = message.get("broker_key")
            if broker_key_hb:
                if broker_key_hb not in self.heartbeat_active or not self.heartbeat_active[broker_key_hb]:
                    self.log_message_received.emit(f"INFO: Heartbeat ativo para {broker_key_hb}")
                    logger.info(f"Heartbeat ativo para {broker_key_hb}")
                    self.heartbeat_active[broker_key_hb] = True
            logger.debug(f"Heartbeat recebido de {broker_key_hb or client_id_hex}")

        elif msg_type == "STREAM" and event == "OHLC_UPDATE":
            stream_data = {
                "ohlc": message.get("ohlc", message.get("", {})),  # Fallback para chave vazia.
                "broker_key": identified_broker_key,
                "request_id": message.get("request_id", ""),
                "timestamp_mql": message.get("timestamp_mql", 0)
            }
            self.stream_ohlc_received.emit(stream_data)
            logger.info(f"Emitido stream_ohlc_received: {stream_data}")

        elif msg_type == "STREAM" and event == "OHLC_INDICATOR_UPDATE":
            data = message.get("data", [])
            for entry in data:
                stream_data = {
                    "symbol": entry.get("symbol", ""),
                    "timeframe": entry.get("timeframe", ""),
                    "ohlc": entry.get("ohlc", {}),
                    "indicators": entry.get("indicators", []),
                    "broker_key": identified_broker_key,
                    "request_id": message.get("request_id", ""),
                    "timestamp_mql": message.get("timestamp_mql", 0)
                }
                self.stream_ohlc_indicators_received.emit(stream_data)
                logger.info(f"Emitido stream_ohlc_indicators_received para {stream_data['symbol']}: {stream_data}")

        elif msg_type == "STREAM" and event == "TRADE_ALLOWED_UPDATE":
            stream_data = {
                "trade_allowed": message.get("trade_allowed", None),
                "broker_key": identified_broker_key,
                "timestamp_mql": message.get("timestamp_mql", 0)
            }
            if identified_broker_key and stream_data["trade_allowed"] is not None:  # Atualiza o buffer global.
                trade_allowed_states[identified_broker_key] = stream_data["trade_allowed"]
            self.trade_allowed_update_received.emit(stream_data)
            logger.info(f"Emitido trade_allowed_update_received: {stream_data}")

        # [FIX 1, 2] Tratamento para eventos de trade via stream.
        elif msg_type == "STREAM" and event == "TRADE_EVENT":
            # O log indica que o dicionário de resultado da operação está sob a chave vazia ''.
            # A parte 'request' pode estar ausente ou também sob a chave 'request', dependendo do EA.
            trade_event_data = {
                "broker_key": identified_broker_key,
                "timestamp_mql": message.get("timestamp_mql", 0),
                "request": message.get("request", {}),  # Assume 'request' está sob a chave 'request' se presente.
                "result": message.get("", {})  # Assume o conteúdo da chave vazia '' é o dicionário de resultado.
            }
            self.trade_event_received.emit(trade_event_data)
            logger.info(f"Emitido trade_event_received: {trade_event_data}")

        # Sub-bloco 3.3 - Respostas a Comandos (RESPONSE)
        elif msg_type == "RESPONSE":
            request_id = message.get("request_id", "")
            status = message.get("status")

            if "ping_" in request_id:
                if status == "OK":
                    original_ts = message.get("original_timestamp", 0)
                    pong_ts_mql = message.get("pong_timestamp_mql", 0)
                    current_ts = time.time()
                    latency_mql_ms = (pong_ts_mql - original_ts) * 1000 if original_ts and pong_ts_mql else 0
                    latency_total_ms = (current_ts - original_ts) * 1000 if original_ts else 0
                    self.log_message_received.emit(
                        f"PONG de {identified_broker_key or client_id_hex}! Lat Total: {latency_total_ms:.1f}ms, Lat MQL: {latency_mql_ms:.1f}ms"
                    )
                    logger.info(f"PONG de {identified_broker_key}: Lat Total: {latency_total_ms:.1f}ms")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(
                        f"ERROR: Resposta PING de {identified_broker_key or client_id_hex} falhou: {error}"
                    )
                    logger.error(f"PING falhou para {identified_broker_key}: {error}")
            elif "get_broker_info_" in request_id:
                if status == "OK":
                    broker_info = {"company": message.get("company", "N/A")}
                    self.broker_info_received.emit(broker_info)
                    logger.info(f"Emitido broker_info_received: {broker_info}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter informações da corretora: {error}")
                    logger.error(f"GET_BROKER_INFO falhou: {error}")
            elif "get_account_info_" in request_id:
                if status == "OK":
                    account_info = {
                        "login": message.get("login", "N/A"),
                        "name": message.get("name", "N/A")
                    }
                    self.account_info_received.emit(account_info)
                    logger.info(f"Emitido account_info_received: {account_info}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter informações da conta: {error}")
                    logger.error(f"GET_ACCOUNT_INFO falhou: {error}")
            elif "get_account_balance_" in request_id:
                if status == "OK":
                    account_balance = {
                        "balance": message.get("balance", "N/A"),
                        "equity": message.get("equity", "N/A"),
                        "currency": message.get("currency", "N/A"),
                        "broker_key": identified_broker_key
                    }
                    self.account_balance_received.emit(account_balance)
                    logger.info(f"Emitido account_balance_received: {account_balance}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter saldo da conta: {error}")
                    logger.error(f"GET_ACCOUNT_BALANCE falhou: {error}")
            elif "get_account_leverage_" in request_id:
                if status == "OK":
                    account_leverage = {"leverage": message.get("leverage", "N/A")}
                    self.account_leverage_received.emit(account_leverage)
                    logger.info(f"Emitido account_leverage_received: {account_leverage}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter alavancagem: {error}")
                    logger.error(f"GET_ACCOUNT_LEVERAGE falhou: {error}")
            elif "get_account_flags_" in request_id:
                if status == "OK":
                    account_flags = {
                        "trade_allowed": message.get("trade_allowed", "N/A"),
                        "expert_enabled": message.get("expert_enabled", "N/A"),
                        "broker_key": identified_broker_key
                    }
                    self.account_flags_received.emit(account_flags)
                    logger.info(f"Emitido account_flags_received: {account_flags}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter flags da conta: {error}")
                    logger.error(f"GET_ACCOUNT_FLAGS falhou: {error}")
            elif "get_account_margin_" in request_id:
                if status == "OK":
                    account_margin = {
                        "margin": message.get("margin", "N/A"),
                        "free_margin": message.get("free_margin", "N/A"),
                        "margin_level": message.get("margin_level", "N/A")
                    }
                    self.account_margin_received.emit(account_margin)
                    logger.info(f"Emitido account_margin_received: {account_margin}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter margem da conta: {error}")
                    logger.error(f"GET_ACCOUNT_MARGIN falhou: {error}")
            elif "get_account_state_" in request_id:
                if status == "OK":
                    account_state = {"account_state": message.get("account_state", "N/A")}
                    self.account_state_received.emit(account_state)
                    logger.info(f"Emitido account_state_received: {account_state}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter estado da conta: {error}")
                    logger.error(f"GET_ACCOUNT_STATE falhou: {error}")
            elif "get_time_server_" in request_id:
                if status == "OK":
                    time_server = {"time_server": message.get("time_server", "N/A")}
                    self.time_server_received.emit(time_server)
                    logger.info(f"Emitido time_server_received: {time_server}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter tempo do servidor: {error}")
                    logger.error(f"GET_TIME_SERVER falhou: {error}")
            elif "get_status_info_" in request_id:
                if status == "OK":
                    original_ts = message.get("original_timestamp", 0)
                    pong_ts_mql = message.get("pong_timestamp_mql", 0)
                    current_ts = time.time()
                    latency_mql_ms = (pong_ts_mql - original_ts) * 1000 if original_ts and pong_ts_mql else 0
                    latency_total_ms = (current_ts - original_ts) * 1000 if original_ts else 0
                    status_info = {
                        "trade_allowed": message.get("trade_allowed", "N/A"),
                        "balance": message.get("balance", "N/A"),
                        "latency": f"{latency_total_ms:.1f}ms",
                        "broker_key": identified_broker_key
                    }
                    self.status_info_received.emit(status_info)
                    logger.info(f"Emitido status_info_received: {status_info}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter informações de status: {error}")
                    logger.error(f"GET_STATUS_INFO falhou: {error}")
            elif "positions_" in request_id:
                if status == "OK":
                    positions_data = message.get("", [])
                    positions = {
                        "data": positions_data,
                        "broker_key": identified_broker_key
                    }
                    self.positions_received.emit(positions)
                    logger.info(
                        f"Emitido positions_received com {len(positions_data)} ordens para {identified_broker_key}.")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter posições: {error}")
                    logger.error(f"POSITIONS falhou: {error}")
            elif "orders_" in request_id:
                if status == "OK":
                    orders = {
                        "orders": message.get("orders", []),
                        "broker_key": identified_broker_key
                    }
                    self.orders_received.emit(orders)
                    logger.info(f"Emitido orders_received: {orders}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter ordens: {error}")
                    logger.error(f"ORDERS falhou: {error}")
            elif "history_data_" in request_id:
                if status == "OK":
                    history_data = {
                        "data": message.get("data", []),
                        "broker_key": identified_broker_key
                    }
                    self.history_data_received.emit(history_data)
                    logger.info(f"Emitido history_data_received: {history_data}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter histórico de dados: {error}")
                    logger.error(f"HISTORY_DATA falhou: {error}")
            elif "history_trades_" in request_id:
                if status == "OK":
                    history_trades = {
                        "trades": message.get("trades", []),
                        "broker_key": identified_broker_key
                    }
                    self.history_trades_received.emit(history_trades)
                    logger.info(f"Emitido history_trades_received: {history_trades}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter histórico de trades: {error}")
                    logger.error(f"HISTORY_TRADES falhou: {error}")
            elif "get_indicator_ma_" in request_id:
                if status == "OK":
                    indicator_data = {
                        "ma_value": message.get("ma_value", "N/A"),
                        "broker_key": identified_broker_key
                    }
                    self.indicator_ma_received.emit(indicator_data)
                    logger.info(f"Emitido indicator_ma_received: {indicator_data}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter Média Móvel: {error}")
                    logger.error(f"GET_INDICATOR_MA falhou: {error}")
            elif "get_ohlc_" in request_id:
                if status == "OK":
                    ohlc_data = {
                        "ohlc": message.get("", {}),
                        "broker_key": identified_broker_key
                    }
                    self.ohlc_received.emit(ohlc_data)
                    logger.info(f"Emitido ohlc_received: {ohlc_data}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter OHLC: {error}")
                    logger.error(f"GET_OHLC falhou: {error}")
            elif "get_tick_" in request_id:
                if status == "OK":
                    tick_data = {
                        "tick": message.get("", {}),
                        "broker_key": identified_broker_key
                    }
                    self.tick_received.emit(tick_data)
                    logger.info(f"Emitido tick_received: {tick_data}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao obter Tick: {error}")
                    logger.error(f"GET_TICK falhou: {error}")
            elif "start_stream_ohlc_" in request_id:
                stream_data = {
                    "status": status,
                    "message": message.get("message", "N/A"),
                    "broker_key": identified_broker_key,
                    "request_id": request_id
                }
                self.stream_ohlc_received.emit(stream_data)
                if status == "OK":
                    logger.info(f"Emitido stream_ohlc_received para START_STREAM_OHLC: {stream_data}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao iniciar streaming OHLC: {error}")
                    logger.error(f"START_STREAM_OHLC falhou: {error}")
            elif "stop_stream_" in request_id:
                stream_data = {
                    "status": status,
                    "message": message.get("message", "N/A"),
                    "broker_key": identified_broker_key,
                    "request_id": request_id
                }
                self.stream_ohlc_received.emit(stream_data)
                if status == "OK":
                    logger.info(f"Emitido stream_ohlc_received para STOP_STREAM: {stream_data}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(f"ERROR: Falha ao parar streaming OHLC: {error}")
                    logger.error(f"STOP_STREAM falhou: {error}")
            elif "trade_" in request_id.lower() or "close_" in request_id.lower() or "modify_" in request_id.lower() or "partial_" in request_id.lower():
                if status == "OK":
                    trade_response = {
                        "result": message.get("result", "Request executed"),
                        "broker_key": identified_broker_key,
                        "status": status,
                        "message": message.get("result", "Request executed"),
                        "request_id": request_id
                    }
                    self.trade_response_received.emit(trade_response)
                    logger.info(f"Emitido trade_response_received: {trade_response}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    trade_response = {
                        "error_message": error,
                        "broker_key": identified_broker_key,
                        "status": status,
                        "request_id": request_id
                    }
                    self.trade_response_received.emit(trade_response)
                    logger.error(f"Comando TRADE_* falhou: {error}")
            else:
                if status == "OK":
                    self.log_message_received.emit(
                        f"INFO: Resposta OK recebida de {identified_broker_key or client_id_hex}: {message}")
                    logger.info(f"Resposta OK desconhecida: {message}")
                else:
                    error = message.get("error_message", "Erro desconhecido")
                    self.log_message_received.emit(
                        f"ERROR: Resposta de {identified_broker_key or client_id_hex}: {error}")
                    logger.error(f"Resposta ERROR desconhecida: {message}")

    # Bloco 4 - Funções de Envio de Comandos
    # Objetivo: Fornecer métodos para enviar comandos específicos ao Expert Advisor (EA) via ZMQ.
    def send_ping(self, broker_key: str):
        """
        Envia um comando PING para o Expert Advisor da corretora especificada.

        Args:
            broker_key (str): A chave da corretora para a qual enviar o PING.
        """
        timestamp = time.time()
        payload = {"timestamp": timestamp}
        self.log_message_received.emit(f"INFO: Enviando PING para {broker_key}...")
        asyncio.create_task(self.zmq_router.send_command_to_broker(
            broker_key, "PING", payload, request_id=f"ping_{broker_key}_{timestamp}"
        ))

    def send_get_status_info(self, broker_key: str):
        """
        Envia um comando GET_STATUS_INFO para o Expert Advisor da corretora especificada.

        Args:
            broker_key (str): A chave da corretora para a qual solicitar informações de status.
        """
        timestamp = time.time()
        payload = {"timestamp": timestamp}
        self.log_message_received.emit(f"INFO: Enviando GET_STATUS_INFO para {broker_key}...")
        asyncio.create_task(self.zmq_router.send_command_to_broker(
            broker_key, "GET_STATUS_INFO", payload, request_id=f"get_status_info_{broker_key}_{int(timestamp)}"
        ))

    # Bloco 5 - Funções Auxiliares
    # Objetivo: Fornecer métodos auxiliares para a manipulação de dados e estados.
    def get_trade_allowed_states(self):
        """
        Retorna uma cópia do buffer global de estados de trade_allowed.

        Returns:
            dict: Um dicionário contendo o status de trade_allowed para cada corretora.
        """
        return trade_allowed_states.copy()

# Arquivo: core/zmq_message_handler.py
# Versão: 1.0.9.k - Envio 4