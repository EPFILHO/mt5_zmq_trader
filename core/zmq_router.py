# core/zmq_router.py
# Versão 1.0.9.i - envio 1
# Ajustes:
# - CORREÇÃO: Adicionada limpeza do estado do cliente em self._clients antes de reconectar sockets em _setup_single_broker_sockets.
# - Adicionados logs de depuração para verificar recebimento de REGISTER após reconexão.
# - Mantida toda a lógica existente da Versão 1.0.9.h, incluindo conexão/desconexão dinâmica, poller e fila de controle.
# - Correção crítica do timeout após reinicialização do MT5, garantindo re-registro do cliente.

import zmq
import zmq.asyncio
import asyncio
import json
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


# Bloco 1 - Inicialização da Classe ZmqRouter
# Objetivo: Definir a classe do roteador ZMQ, inicializar atributos e preparar o contexto ZMQ.
class ZmqRouter:
    def __init__(self, broker_manager):
        logger.debug("Bloco 1 - ZmqRouter.__init__ chamado.")
        self.broker_manager = broker_manager
        self._running = False
        self._message_handler = None
        self._clients = {}  # Mapeia broker_key para zmq_id_bytes (hex)
        self._responses = {}
        self._response_events = {}
        logger.debug("Bloco 1 - Criando contexto ZMQ asyncio...")
        self.context = zmq.asyncio.Context()
        logger.debug("Bloco 1 - Contexto ZMQ criado.")

        # Dicionários para armazenar sockets ativos por broker_key
        self.sockets = {}  # AdminPort (ZMQ_DEALER)
        self.data_sockets = {}  # DataPort (ZMQ_DEALER)
        self.live_sockets = {}  # LivePort (ZMQ_SUB)
        self.trade_sockets = {}  # TradePort (ZMQ_DEALER - para futura migração de comandos de trade)
        self.stream_sockets = {}  # StrPort (ZMQ_SUB - para futura migração de streams)

        # Fila para comandos de controle de socket (thread-safe)
        self._socket_control_queue = asyncio.Queue()
        # Poller principal para monitorar todos os sockets ZMQ
        self._poller = zmq.asyncio.Poller()
        logger.debug("Bloco 1 - ZmqRouter.__init__ concluído.")

    # Bloco 2 - Gerenciamento de Sockets (Enviadores de Comandos para a Fila)
    # Objetivo: Métodos assíncronos para adicionar comandos de conexão/desconexão à fila de controle.
    async def connect_broker_sockets(self, broker_key: str, broker_config: dict):
        """
        Adiciona um comando à fila para conectar os sockets ZMQ de uma corretora.
        Esta função é chamada de fora do loop de eventos do ZmqRouter.
        """
        logger.info(f"Bloco 2 - Adicionando comando CONNECT para {broker_key} à fila de controle.")
        await self._socket_control_queue.put(("CONNECT", broker_key, broker_config))

    async def disconnect_broker_sockets(self, broker_key: str):
        """
        Adiciona um comando à fila para desconectar os sockets ZMQ de uma corretora.
        Esta função é chamada de fora do loop de eventos do ZmqRouter.
        """
        logger.info(f"Bloco 2 - Adicionando comando DISCONNECT para {broker_key} à fila de controle.")
        await self._socket_control_queue.put(("DISCONNECT", broker_key))

    # Bloco 3 - Parada do Router
    # Objetivo: Parar o roteador ZMQ de forma controlada, fechando todos os sockets.
    async def stop(self):
        logger.info("Bloco 3 - Solicitando parada do ZmqRouter...")
        self._running = False

        # Sinaliza todos os eventos de resposta pendentes
        for event in self._response_events.values():
            event.set()

        # Envia comandos de desconexão para todos os brokers ativos para que o _receive_loop os feche
        all_broker_keys = set(self.sockets.keys()) | \
                          set(self.data_sockets.keys()) | \
                          set(self.live_sockets.keys()) | \
                          set(self.trade_sockets.keys()) | \
                          set(self.stream_sockets.keys())

        for broker_key in all_broker_keys:
            if broker_key in self._clients:  # Apenas se o broker estiver registrado
                logger.debug(f"Bloco 3 - Enviando comando DISCONNECT para {broker_key} durante o shutdown.")
                await self._socket_control_queue.put(("DISCONNECT", broker_key))

        # Dá um tempo para o _receive_loop processar os comandos de desconexão
        try:
            await asyncio.wait_for(self._socket_control_queue.join(), timeout=2.0)
            logger.info("Bloco 3 - Fila de controle de sockets processada durante o shutdown.")
        except asyncio.TimeoutError:
            logger.warning("Bloco 3 - Timeout ao esperar a fila de controle de sockets esvaziar.")
        except Exception as e:
            logger.error(f"Bloco 3 - Erro ao esperar a fila de controle de sockets: {e}")

        # Fechar o contexto ZMQ (isso fechará todos os sockets restantes)
        self.context.term()
        logger.info("Bloco 3 - Contexto ZMQ terminado.")

    # Bloco 4 - Envio de Mensagens ZMQ
    # Objetivo: Funções auxiliares para enviar mensagens JSON através dos sockets ZMQ.
    async def _send_message(self, message_dict: dict, broker_key: str, port_type: str):
        """
        Envia uma mensagem JSON para um broker específico através do socket da porta especificada.
        """
        socket_map = {
            'AdminPort': self.sockets,
            'DataPort': self.data_sockets,
            'LivePort': self.live_sockets,  # LivePort é PUB no EA, SUB no Python, não envia comandos
            'TradePort': self.trade_sockets,
            'StrPort': self.stream_sockets  # StrPort é PUB no EA, SUB no Python, não envia comandos
        }
        target_socket_dict = socket_map.get(port_type)

        if not target_socket_dict or broker_key not in target_socket_dict:
            logger.error(f"Bloco 4 - Socket {port_type} não configurado ou não conectado para {broker_key}.")
            return

        target_socket = target_socket_dict[broker_key]
        if target_socket.closed:
            logger.warning(
                f"Bloco 4 - Tentativa de enviar mensagem para socket fechado ({port_type}) para {broker_key}.")
            return

        try:
            message_str = json.dumps(message_dict)
            logger.debug(f"Bloco 4 - ZMQ TX para {broker_key} ({port_type}): {message_str}")
            await target_socket.send(message_str.encode('utf-8'))
        except zmq.ZMQError as e:
            logger.error(f"Bloco 4 - Erro ZMQ ao enviar mensagem para {broker_key} ({port_type}): {e}")
        except Exception as e:
            logger.exception(f"Bloco 4 - Erro inesperado ao enviar mensagem para {broker_key} ({port_type}): {e}")

    async def send_command_to_broker(self, broker_key: str, command: str, payload: dict = None, request_id: str = None,
                                     use_data_port: bool = False, use_trade_port: bool = False):
        """
        Envia um comando para um broker específico e aguarda a resposta.
        Permite escolher entre AdminPort, DataPort ou TradePort.
        """
        if not broker_key:
            logger.error("Bloco 4 - send_command_to_broker chamado sem broker_key.")
            return {"status": "ERROR", "message": "broker_key não fornecida."}

        port_type = 'AdminPort'
        if use_data_port:
            port_type = 'DataPort'
        elif use_trade_port:
            port_type = 'TradePort'

        target_socket_dict = {
            'AdminPort': self.sockets,
            'DataPort': self.data_sockets,
            'TradePort': self.trade_sockets
        }.get(port_type)

        if not target_socket_dict or broker_key not in target_socket_dict or target_socket_dict[broker_key].closed:
            logger.error(
                f"Bloco 4 - Tentativa de enviar comando para broker_key não conectado ou socket fechado ({port_type}): {broker_key}")
            return {"status": "ERROR",
                    "message": f"Corretora {broker_key} não conectada ou socket {port_type} fechado."}

        request_id = request_id or f"{command.lower()}_{broker_key}_{int(time.time())}"
        message = {
            "type": "REQUEST",
            "command": command,
            "request_id": request_id,
            "broker_key": broker_key
        }
        if payload:
            message["payload"] = payload

        self._response_events[request_id] = asyncio.Event()
        try:
            await self._send_message(message, broker_key, port_type)
            logger.info(
                f"Bloco 4 - Comando {command} enviado para {broker_key} com request_id: {request_id} ({port_type})")
            await asyncio.wait_for(self._response_events[request_id].wait(), timeout=5.0)
            if request_id in self._responses:
                response = self._responses.pop(request_id)
                logger.info(f"Bloco 4 - Resposta recebida para {command} de {broker_key}: {response}")
                return response
            else:
                logger.error(f"Bloco 4 - Resposta não encontrada para {command} com request_id: {request_id}")
                return {"status": "ERROR", "message": "Resposta não recebida"}
        except asyncio.TimeoutError:
            logger.error(f"Bloco 4 - Timeout ao aguardar resposta para {command} de {broker_key}")
            return {"status": "ERROR", "message": "Timeout na resposta"}
        except Exception as e:
            logger.error(f"Bloco 4 - Erro ao enviar {command} para {broker_key}: {str(e)}")
            return {"status": "ERROR", "message": str(e)}
        finally:
            if request_id in self._response_events:
                del self._response_events[request_id]

    # Bloco 5 - Processamento de Mensagens Recebidas
    # Objetivo: Deserializar e rotear mensagens ZMQ recebidas para o manipulador de mensagens.
    async def _process_message(self, message_data: dict, broker_key: str):
        logger.debug(f"Bloco 5 - ZMQ RX [{broker_key}]: {message_data}")
        msg_type = message_data.get("type")
        event = message_data.get("event")
        broker_key_msg = message_data.get("broker_key")
        request_id = message_data.get("request_id")

        if msg_type == "SYSTEM" and event == "REGISTER":
            logger.debug(f"Bloco 5 - Processando REGISTER para broker_key: {broker_key_msg}")
            if broker_key_msg:
                if broker_key_msg in self._clients and self._clients[broker_key_msg] != broker_key:
                    logger.warning(
                        f"Bloco 5 - BrokerKey {broker_key_msg} já estava registrada, atualizando para {broker_key}.")
                elif broker_key_msg not in self._clients:
                    logger.info(f"Bloco 5 - Registrando novo cliente: {broker_key_msg} para {broker_key}")
                self._clients[broker_key_msg] = broker_key
                if self._message_handler:
                    await self._message_handler.handle_zmq_message(broker_key.encode('utf-8'), message_data)
            else:
                logger.warning(f"Bloco 5 - Recebida mensagem REGISTER sem broker_key")
        elif msg_type == "SYSTEM" and event == "UNREGISTER":
            removed_key = None
            if broker_key_msg and broker_key_msg in self._clients and self._clients[broker_key_msg] == broker_key:
                logger.info(f"Bloco 5 - Desregistrando cliente: {broker_key_msg}")
                del self._clients[broker_key_msg]
                removed_key = broker_key_msg
            elif broker_key_msg:
                logger.warning(
                    f"Bloco 5 - Recebido UNREGISTER para {broker_key_msg}, mas não corresponde ao registro atual.")
            else:
                for key, zid in list(self._clients.items()):
                    if zid == broker_key:
                        logger.warning(f"Bloco 5 - Recebido UNREGISTER sem broker_key, removendo {key}")
                        del self._clients[key]
                        removed_key = key
                        break
                if not removed_key:
                    logger.warning(f"Bloco 5 - Recebido UNREGISTER e {broker_key} não encontrado nos registros.")
            if self._message_handler:
                unregister_notification = {
                    "type": "INTERNAL",
                    "event": "CLIENT_UNREGISTERED",
                    "broker_key": removed_key,
                    "zmq_id_hex": broker_key
                }
                await self._message_handler.handle_zmq_message(broker_key.encode('utf-8'), unregister_notification)
        elif msg_type == "RESPONSE" or msg_type == "STREAM":
            if request_id and msg_type == "RESPONSE":
                self._responses[request_id] = message_data
                if request_id in self._response_events:
                    self._response_events[request_id].set()
            if self._message_handler:
                await self._message_handler.handle_zmq_message(broker_key.encode('utf-8'), message_data)
            else:
                logger.warning(f"Bloco 5 - Recebida {msg_type} mas nenhum message_handler configurado: {message_data}")
        else:
            logger.warning(f"Bloco 5 - Tipo/Evento de mensagem não tratado recebido de {broker_key}: {message_data}")
            if self._message_handler:
                await self._message_handler.handle_zmq_message(broker_key.encode('utf-8'), message_data)

    # Bloco 6 - Funções Principais do Router (_receive_loop e auxiliares)
    # Objetivo: Gerenciar o loop de recebimento de mensagens ZMQ e os comandos de controle de socket.
    async def _setup_single_broker_sockets(self, broker_key: str, config: dict):
        """Cria e conecta sockets ZMQ para uma única corretora e os registra no poller."""
        logger.info(f"Bloco 6 - Configurando sockets para {broker_key}...")

        # Limpa estado residual do cliente antes de reconectar
        if broker_key in self._clients:
            logger.debug(f"Bloco 6 - Removendo estado residual do cliente {broker_key} antes da reconexão.")
            del self._clients[broker_key]

        # Fecha sockets existentes para evitar duplicatas
        await self._teardown_single_broker_sockets(broker_key)

        # AdminPort (DEALER)
        admin_port = config.get('admin_port')
        if admin_port:
            admin_address = f"tcp://127.0.0.1:{admin_port}"
            socket = self.context.socket(zmq.DEALER)
            try:
                socket.connect(admin_address)
                socket.setsockopt(zmq.LINGER, 0)
                self._poller.register(socket, zmq.POLLIN)
                self.sockets[broker_key] = socket
                logger.info(f"Bloco 6 - SUCESSO: ZMQ DEALER conectado a {admin_address} para {broker_key} (AdminPort)")
            except zmq.ZMQError as e:
                logger.error(f"Bloco 6 - Erro ao conectar AdminPort para {broker_key} em {admin_address}: {e}")
                socket.close()
        else:
            logger.warning(f"Bloco 6 - Porta administrativa não definida para {broker_key}")

        # DataPort (DEALER)
        data_port = config.get('data_port')
        if data_port:
            data_address = f"tcp://127.0.0.1:{data_port}"
            data_socket = self.context.socket(zmq.DEALER)
            try:
                data_socket.connect(data_address)
                data_socket.setsockopt(zmq.LINGER, 0)
                self._poller.register(data_socket, zmq.POLLIN)
                self.data_sockets[broker_key] = data_socket
                logger.info(f"Bloco 6 - SUCESSO: ZMQ DEALER conectado a {data_address} para {broker_key} (DataPort)")
            except zmq.ZMQError as e:
                logger.error(f"Bloco 6 - Erro ao conectar DataPort para {broker_key} em {data_address}: {e}")
                data_socket.close()
        else:
            logger.warning(f"Bloco 6 - Porta de dados não definida para {broker_key}")

        # LivePort (SUB)
        live_port = config.get('live_port')
        if live_port:
            live_address = f"tcp://127.0.0.1:{live_port}"
            live_socket = self.context.socket(zmq.SUB)
            try:
                live_socket.connect(live_address)
                live_socket.setsockopt_string(zmq.SUBSCRIBE, "")
                live_socket.setsockopt(zmq.LINGER, 0)
                self._poller.register(live_socket, zmq.POLLIN)
                self.live_sockets[broker_key] = live_socket
                logger.info(f"Bloco 6 - SUCESSO: ZMQ SUB conectado a {live_address} para {broker_key} (LivePort)")
            except zmq.ZMQError as e:
                logger.error(f"Bloco 6 - Erro ao conectar LivePort para {broker_key} em {live_address}: {e}")
                live_socket.close()
        else:
            logger.warning(f"Bloco 6 - Porta de streaming (LivePort) não definida para {broker_key}")

        # TradePort (DEALER - para futura migração de comandos de trade)
        trade_port = config.get('trade_port')
        if trade_port:
            trade_address = f"tcp://127.0.0.1:{trade_port}"
            trade_socket = self.context.socket(zmq.DEALER)
            try:
                trade_socket.connect(trade_address)
                trade_socket.setsockopt(zmq.LINGER, 0)
                self._poller.register(trade_socket, zmq.POLLIN)
                self.trade_sockets[broker_key] = trade_socket
                logger.info(f"Bloco 6 - SUCESSO: ZMQ DEALER conectado a {trade_address} para {broker_key} (TradePort)")
            except zmq.ZMQError as e:
                logger.error(f"Bloco 6 - Erro ao conectar TradePort para {broker_key} em {trade_address}: {e}")
                trade_socket.close()
        else:
            logger.warning(f"Bloco 6 - Porta de trade (TradePort) não definida para {broker_key}")

        # StrPort (SUB - para futura migração de streams)
        str_port = config.get('str_port')
        if str_port:
            str_address = f"tcp://127.0.0.1:{str_port}"
            stream_socket = self.context.socket(zmq.SUB)
            try:
                stream_socket.connect(str_address)
                stream_socket.setsockopt_string(zmq.SUBSCRIBE, "")
                stream_socket.setsockopt(zmq.LINGER, 0)
                self._poller.register(stream_socket, zmq.POLLIN)
                self.stream_sockets[broker_key] = stream_socket
                logger.info(f"Bloco 6 - SUCESSO: ZMQ SUB conectado a {str_address} para {broker_key} (StrPort)")
            except zmq.ZMQError as e:
                logger.error(f"Bloco 6 - Erro ao conectar StrPort para {broker_key} em {str_address}: {e}")
                stream_socket.close()
        else:
            logger.warning(f"Bloco 6 - Porta de streaming (StrPort) não definida para {broker_key}")

    async def _teardown_single_broker_sockets(self, broker_key: str):
        """Fecha e desregistra os sockets ZMQ de uma única corretora do poller."""
        logger.info(f"Bloco 6 - Desconectando sockets para {broker_key}...")
        socket_types = {
            'AdminPort': self.sockets,
            'DataPort': self.data_sockets,
            'LivePort': self.live_sockets,
            'TradePort': self.trade_sockets,
            'StrPort': self.stream_sockets
        }

        for port_name, socket_dict in socket_types.items():
            socket = socket_dict.pop(broker_key, None)
            if socket:
                try:
                    if not socket.closed:
                        self._poller.unregister(socket)
                        socket.close()
                        logger.info(f"Bloco 6 - Socket {port_name} para {broker_key} fechado e desregistrado.")
                    else:
                        logger.warning(f"Bloco 6 - Socket {port_name} para {broker_key} já estava fechado.")
                except KeyError:
                    logger.warning(
                        f"Bloco 6 - Socket {port_name} para {broker_key} não encontrado no poller para desregistro.")
                except zmq.ZMQError as e:
                    logger.error(f"Bloco 6 - Erro ZMQ ao desregistrar/fechar socket {port_name} para {broker_key}: {e}")
                except Exception as e:
                    logger.exception(
                        f"Bloco 6 - Erro inesperado ao desregistrar/fechar socket {port_name} para {broker_key}: {e}")
            else:
                logger.debug(
                    f"Bloco 6 - Socket {port_name} para {broker_key} não encontrado para desconexão (já removido?).")

    async def _receive_loop(self):
        logger.info("Bloco 6 - ==> Iniciado loop de recebimento ZMQ (_receive_loop).")
        self._running = True
        while self._running:
            try:
                # Monitora sockets ZMQ e a fila de controle de sockets
                socks = dict(await self._poller.poll(100))  # Poll por 100ms

                # Processa comandos da fila de controle
                while not self._socket_control_queue.empty():
                    command_type, broker_key, *args = await self._socket_control_queue.get()
                    if command_type == "CONNECT":
                        broker_config = args[0]
                        await self._setup_single_broker_sockets(broker_key, broker_config)
                    elif command_type == "DISCONNECT":
                        await self._teardown_single_broker_sockets(broker_key)
                    self._socket_control_queue.task_done()
                    logger.debug(f"Bloco 6 - Comando de controle '{command_type}' para {broker_key} processado.")

                # Processa mensagens dos sockets ZMQ
                for socket_type_map, port_name in [
                    (self.sockets, 'AdminPort'),
                    (self.data_sockets, 'DataPort'),
                    (self.live_sockets, 'LivePort'),
                    (self.trade_sockets, 'TradePort'),
                    (self.stream_sockets, 'StrPort')
                ]:
                    for broker_key, socket in socket_type_map.items():
                        if socket in socks and socks[socket] == zmq.POLLIN:
                            try:
                                message_body_bytes = await socket.recv()
                                message_str = message_body_bytes.decode('utf-8', errors='ignore')
                                if not message_str.startswith('{') or not message_str.endswith('}'):
                                    logger.warning(
                                        f"Bloco 6 - Mensagem JSON incompleta ou malformada (antes da correção) de {broker_key} ({port_name}): {message_str}")
                                    if not message_str.startswith('{'):
                                        message_str = '{' + message_str
                                    if not message_str.endswith('}'):
                                        message_str += '}'
                                    logger.warning(f"Bloco 6 - Mensagem JSON corrigida: {message_str}")
                                try:
                                    message_data = json.loads(message_str)
                                    await self._process_message(message_data, broker_key)
                                except json.JSONDecodeError as e:
                                    logger.error(
                                        f"Bloco 6 - Erro ao decodificar JSON de {broker_key} ({port_name}): {message_str}. Erro: {e}")
                                except UnicodeDecodeError as e:
                                    logger.error(
                                        f"Bloco 6 - Erro ao decodificar UTF-8 de {broker_key} ({port_name}): {message_body_bytes}. Erro: {e}")
                                except Exception as e_proc:
                                    logger.exception(
                                        f"Bloco 6 - Erro ao processar mensagem de {broker_key} ({port_name}): {e_proc}")
                            except zmq.ZMQError as e:
                                if e.errno == zmq.ETERM:
                                    logger.info("Bloco 6 - Contexto ZMQ terminado, encerrando loop de recebimento.")
                                    self._running = False
                                    break
                                elif e.errno == zmq.EAGAIN:
                                    logger.debug("Bloco 6 - recv retornou EAGAIN, tentando novamente...")
                                else:
                                    logger.exception(
                                        f"Bloco 6 - Erro ZMQ inesperado ao receber de {broker_key} ({port_name}): {e}")
                            except Exception as e:
                                logger.exception(
                                    f"Bloco 6 - Erro inesperado ao receber de {broker_key} ({port_name}): {e}")

            except zmq.ZMQError as e:
                if e.errno == zmq.ETERM:
                    logger.info("Bloco 6 - Contexto ZMQ terminado, encerrando loop de recebimento.")
                    break
                elif e.errno == zmq.EAGAIN:
                    logger.debug("Bloco 6 - poller.poll retornou EAGAIN, tentando novamente...")
                    await asyncio.sleep(0.01)
                else:
                    logger.exception(f"Bloco 6 - Erro ZMQ inesperado no loop _receive_loop: {e}")
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                logger.info("Bloco 6 - Loop _receive_loop cancelado.")
                break
            except Exception as e:
                logger.exception(f"Bloco 6 - Erro inesperado na iteração do loop _receive_loop: {e}")
                await asyncio.sleep(0.5)
        logger.info(f"Bloco 6 - <== Loop de recebimento ZMQ (_receive_loop) finalizado.")

    async def run(self, message_handler):
        logger.info(f"Bloco 6 - >>> ZmqRouter.run() INICIADO.")
        self._message_handler = message_handler
        self._running = True
        receive_task = None
        try:
            logger.debug("Bloco 6 - Criando tarefa _receive_loop...")
            receive_task = asyncio.create_task(self._receive_loop())
            logger.debug("Bloco 6 - Tarefa _receive_loop criada. Aguardando sua conclusão (await)...")
            await receive_task
        except asyncio.CancelledError:
            logger.info("Bloco 6 - Tarefa ZmqRouter.run() cancelada.")
            self._running = False
            if receive_task and not receive_task.done():
                receive_task.cancel()
        except Exception as e:
            logger.exception(f"Bloco 6 - !!! ERRO CRÍTICO inesperado em ZmqRouter.run(): {e}")
            self._running = False
        finally:
            logger.info("Bloco 6 - <<< ZmqRouter.run() FINALIZADO (bloco finally).")

# core/zmq_router.py
# Versão 1.0.9.i - envio 1