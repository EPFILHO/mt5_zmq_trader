# core/zmq_router.py
# Versão 1.0.9.a - Envio 6
# Ajustes:
# - Adicionado logging detalhado em _configure_sockets para mostrar a porta exata (ex.: tcp://127.0.0.1:15561).
# - Adicionado logging em _send_message para confirmar a porta usada no envio.
# - Mantidas todas as alterações do Envio 5 (1.0.9.a):
#   - Suporte para DataPort via self.data_sockets.
#   - Parâmetro use_data_port em send_command_to_broker.
#   - Polling de AdminPort e DataPort em _receive_loop.
# - Mantidas todas as funcionalidades do Envio 4:
#   - Configuração de sockets ZMQ DEALER para AdminPort.
#   - Processamento de mensagens SYSTEM, RESPONSE, etc.
#   - Registro/desregistro de clientes, respostas com request_id.
# - Alterações mínimas para depuração e validação de portas, sem afetar comandos existentes.

import zmq
import zmq.asyncio
import asyncio
import json
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

class ZmqRouter:
    def __init__(self, broker_manager):
        logger.debug("ZmqRouter.__init__ chamado.")
        self.broker_manager = broker_manager
        self.brokers = self.broker_manager.get_brokers()  # Obtém configurações de corretoras
        self._running = False
        self._message_handler = None
        self._clients = {}  # Mapeia broker_key -> zmq_id
        self._responses = {}  # Mapeia request_id -> resposta recebida
        self._response_events = {}  # Mapeia request_id -> asyncio.Event para sinalizar resposta

        logger.debug("Criando contexto ZMQ asyncio...")
        self.context = zmq.asyncio.Context()
        logger.debug("Contexto ZMQ criado.")
        self.sockets = {}  # Mapeia broker_key -> socket ZMQ (AdminPort)
        self.data_sockets = {}  # Mapeia broker_key -> socket ZMQ (DataPort)
        self._configure_sockets()
        logger.debug("ZmqRouter.__init__ concluído.")

    def _configure_sockets(self):
        """Configura sockets ZMQ para AdminPort e DataPort, usando CONNECT."""
        for broker_key, config in self.brokers.items():
            # Configurar socket para AdminPort
            admin_port = config.get('admin_port')
            if not admin_port:
                logger.error(f"Porta administrativa não definida para {broker_key}")
                continue
            admin_address = f"tcp://127.0.0.1:{admin_port}"
            socket = self.context.socket(zmq.DEALER)
            try:
                socket.connect(admin_address)
                logger.info(f"[CONNECT] SUCESSO: ZMQ DEALER conectado a {admin_address} para {broker_key} (AdminPort)")
                self.sockets[broker_key] = socket
            except zmq.ZMQError as e:
                logger.error(f"Erro ao conectar socket AdminPort para {broker_key} em {admin_address}: {e}")
                socket.close()

            # Configurar socket para DataPort
            data_port = config.get('data_port')
            if not data_port:
                logger.error(f"Porta de dados não definida para {broker_key}. Verifique o config.ini.")
                continue
            data_address = f"tcp://127.0.0.1:{data_port}"
            data_socket = self.context.socket(zmq.DEALER)
            try:
                data_socket.connect(data_address)
                logger.info(f"[CONNECT] SUCESSO: ZMQ DEALER conectado a {data_address} para {broker_key} (DataPort)")
                self.data_sockets[broker_key] = data_socket
            except zmq.ZMQError as e:
                logger.error(f"Erro ao conectar socket DataPort para {broker_key} em {data_address}: {e}")
                data_socket.close()

    def stop(self):
        logger.info("Solicitando parada do ZmqRouter...")
        self._running = False
        # Sinalizar todos os eventos pendentes para evitar travamento
        for event in self._response_events.values():
            event.set()
        # Fechar todos os sockets (AdminPort e DataPort)
        for socket in self.sockets.values():
            if not socket.closed:
                socket.close()
        for socket in self.data_sockets.values():
            if not socket.closed:
                socket.close()
        logger.info("Sockets ZMQ fechados.")

    async def _send_message(self, message_dict, broker_key, use_data_port=False):
        """Envia uma mensagem para o EA via socket ZMQ específico."""
        socket_dict = self.data_sockets if use_data_port else self.sockets
        port_type = 'DataPort' if use_data_port else 'AdminPort'
        if broker_key not in socket_dict:
            logger.error(f"Socket {port_type} não configurado para {broker_key}")
            return

        try:
            message_str = json.dumps(message_dict)
            # Obter a porta do socket para logging
            address = f"tcp://127.0.0.1:{self.brokers[broker_key].get('data_port' if use_data_port else 'admin_port')}"
            logger.debug(f"ZMQ TX para {broker_key} ({port_type}, {address}): {message_str}")
            await socket_dict[broker_key].send(message_str.encode('utf-8'))
        except zmq.ZMQError as e:
            logger.error(f"Erro ZMQ ao enviar mensagem para {broker_key} ({port_type}, {address}): {e}")
        except Exception as e:
            logger.exception(f"Erro inesperado ao enviar mensagem para {broker_key} ({port_type}, {address}): {e}")

    async def send_command_to_broker(self, broker_key, command, payload=None, request_id=None, use_data_port=False):
        """Envia um comando para um EA específico identificado pela broker_key e aguarda a resposta."""
        socket_dict = self.data_sockets if use_data_port else self.sockets
        port_type = 'DataPort' if use_data_port else 'AdminPort'
        if broker_key not in socket_dict:
            logger.error(f"Tentativa de enviar comando para broker_key não configurada ({port_type}): {broker_key}")
            return {"status": "ERROR", "message": "Corretora não conectada"}

        request_id = request_id or f"{command.lower()}_{broker_key}_{int(time.time())}"
        message = {
            "type": "REQUEST",
            "command": command,
            "request_id": request_id,
            "broker_key": broker_key
        }
        if payload:
            message["payload"] = payload

        # Criar evento para aguardar resposta
        self._response_events[request_id] = asyncio.Event()

        try:
            await self._send_message(message, broker_key, use_data_port=use_data_port)
            address = f"tcp://127.0.0.1:{self.brokers[broker_key].get('data_port' if use_data_port else 'admin_port')}"
            logger.info(f"Comando {command} enviado para {broker_key} com request_id: {request_id} ({port_type}, {address})")

            # Aguardar resposta com timeout de 5 segundos
            await asyncio.wait_for(self._response_events[request_id].wait(), timeout=5.0)
            if request_id in self._responses:
                response = self._responses.pop(request_id)
                logger.info(f"Resposta recebida para {command} de {broker_key}: {response}")
                return response
            else:
                logger.error(f"Resposta não encontrada para {command} com request_id: {request_id}")
                return {"status": "ERROR", "message": "Resposta não recebida"}
        except asyncio.TimeoutError:
            logger.error(f"Timeout ao aguardar resposta para {command} de {broker_key}")
            return {"status": "ERROR", "message": "Timeout na resposta"}
        except Exception as e:
            logger.error(f"Erro ao enviar {command} para {broker_key}: {str(e)}")
            return {"status": "ERROR", "message": str(e)}
        finally:
            # Limpar evento
            if request_id in self._response_events:
                del self._response_events[request_id]

    async def _process_message(self, message_data, broker_key):
        """Processa uma mensagem recebida."""
        logger.debug(f"ZMQ RX [{broker_key}] Data: {message_data}")
        msg_type = message_data.get("type")
        event = message_data.get("event")
        broker_key_msg = message_data.get("broker_key")
        request_id = message_data.get("request_id")

        if msg_type == "SYSTEM" and event == "REGISTER":
            if broker_key_msg:
                if broker_key_msg in self._clients and self._clients[broker_key_msg] != broker_key:
                    logger.warning(f"BrokerKey {broker_key_msg} já estava registrada, atualizando para {broker_key}.")
                elif broker_key_msg not in self._clients:
                    logger.info(f"Registrando novo cliente: {broker_key_msg} para {broker_key}")
                self._clients[broker_key_msg] = broker_key
                if self._message_handler:
                    await self._message_handler.handle_zmq_message(broker_key.encode('utf-8'), message_data)
            else:
                logger.warning(f"Recebida mensagem REGISTER sem broker_key")

        elif msg_type == "SYSTEM" and event == "UNREGISTER":
            removed_key = None
            if broker_key_msg and broker_key_msg in self._clients and self._clients[broker_key_msg] == broker_key:
                logger.info(f"Desregistrando cliente: {broker_key_msg}")
                del self._clients[broker_key_msg]
                removed_key = broker_key_msg
            elif broker_key_msg:
                logger.warning(f"Recebido UNREGISTER para {broker_key_msg}, mas não corresponde ao registro atual.")
            else:
                for key, zid in list(self._clients.items()):
                    if zid == broker_key:
                        logger.warning(f"Recebido UNREGISTER sem broker_key, removendo {key}")
                        del self._clients[key]
                        removed_key = key
                        break
                if not removed_key:
                    logger.warning(f"Recebido UNREGISTER e {broker_key} não encontrado nos registros.")

            if self._message_handler:
                unregister_notification = {
                    "type": "INTERNAL",
                    "event": "CLIENT_UNREGISTERED",
                    "broker_key": removed_key,
                    "zmq_id_hex": broker_key
                }
                await self._message_handler.handle_zmq_message(broker_key.encode('utf-8'), unregister_notification)

        elif msg_type == "RESPONSE":
            # Armazenar resposta para send_command_to_broker
            if request_id:
                self._responses[request_id] = message_data
                if request_id in self._response_events:
                    self._response_events[request_id].set()
            if self._message_handler:
                await self._message_handler.handle_zmq_message(broker_key.encode('utf-8'), message_data)
            else:
                logger.warning(f"Recebida RESPONSE mas nenhum message_handler configurado: {message_data}")

        else:
            logger.warning(f"Tipo/Evento de mensagem não tratado recebido de {broker_key}: {message_data}")
            if self._message_handler:
                await self._message_handler.handle_zmq_message(broker_key.encode('utf-8'), message_data)

    async def _receive_loop(self):
        """Loop principal para receber mensagens ZMQ de todos os sockets."""
        logger.info("==> Iniciado loop de recebimento ZMQ (_receive_loop).")
        poller = zmq.asyncio.Poller()
        for broker_key, socket in self.sockets.items():
            poller.register(socket, zmq.POLLIN)
        for broker_key, socket in self.data_sockets.items():
            poller.register(socket, zmq.POLLIN)
        self._running = True

        while self._running:
            try:
                logger.debug("--- Topo do loop while _running ---")
                logger.debug("Aguardando mensagens ZMQ (await poller.poll)...")
                events = await poller.poll(1000)  # Timeout de 1 segundo
                for socket, event in events:
                    if event & zmq.POLLIN:
                        # Identificar se o socket é AdminPort ou DataPort
                        broker_key = None
                        port_type = None
                        address = None
                        for key, s in self.sockets.items():
                            if s == socket:
                                broker_key = key
                                port_type = "AdminPort"
                                address = f"tcp://127.0.0.1:{self.brokers[key].get('admin_port')}"
                                break
                        if not broker_key:
                            for key, s in self.data_sockets.items():
                                if s == socket:
                                    broker_key = key
                                    port_type = "DataPort"
                                    address = f"tcp://127.0.0.1:{self.brokers[key].get('data_port')}"
                                    break
                        if not broker_key:
                            logger.error("Socket não encontrado nos registros.")
                            continue
                        logger.debug(f"Mensagem recebida no socket de {broker_key} ({port_type}, {address})")
                        message_body_bytes = await socket.recv()
                        message_str = message_body_bytes.decode('utf-8', errors='ignore')
                        # Verificação e correção de JSON incompleto
                        if not message_str.startswith('{') or not message_str.endswith('}'):
                            logger.warning(f"Mensagem JSON incompleta ou malformada (antes da correção): {message_str}")
                            if not message_str.startswith('{'):
                                message_str = '{' + message_str
                            if not message_str.endswith('}'):
                                message_str += '}'
                            logger.warning(f"Mensagem JSON corrigida: {message_str}")
                        try:
                            message_data = json.loads(message_str)
                            # Processa a mensagem recebida
                            await self._process_message(message_data, broker_key)

                        except json.JSONDecodeError as e:
                            logger.error(f"Erro ao decodificar JSON de {broker_key}: {message_str}. Erro: {e}")
                        except UnicodeDecodeError as e:
                            logger.error(f"Erro ao decodificar UTF-8 de {broker_key}: {message_body_bytes}. Erro: {e}")
                        except Exception as e_proc:
                            logger.exception(f"Erro ao processar mensagem de {broker_key}: {e_proc}")

            except zmq.ZMQError as e:
                if e.errno == zmq.ETERM:
                    logger.info("Contexto ZMQ terminado, encerrando loop de recebimento.")
                    break
                elif e.errno == zmq.EAGAIN:
                    logger.debug("recv retornou EAGAIN, tentando novamente...")
                    await asyncio.sleep(0.01)
                else:
                    logger.exception(f"Erro ZMQ inesperado no loop _receive_loop: {e}")
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                logger.info("Loop _receive_loop cancelado.")
                break
            except Exception as e:
                logger.exception(f"Erro inesperado na iteração do loop _receive_loop: {e}")
                await asyncio.sleep(0.5)

        logger.info(f"<== Loop de recebimento ZMQ (_receive_loop) finalizado.")

    async def run(self, message_handler):
        """Inicia o ZMQ Router e o loop de recebimento."""
        logger.info(f">>> ZmqRouter.run() INICIADO.")
        self._message_handler = message_handler
        self._running = True
        receive_task = None

        try:
            logger.debug("Criando tarefa _receive_loop...")
            receive_task = asyncio.create_task(self._receive_loop())
            logger.debug("Tarefa _receive_loop criada. Aguardando sua conclusão (await)...")
            await receive_task

        except asyncio.CancelledError:
            logger.info("Tarefa ZmqRouter.run() cancelada.")
            self._running = False
            if receive_task and not receive_task.done():
                receive_task.cancel()
        except Exception as e:
            logger.exception(f"!!! ERRO CRÍTICO inesperado em ZmqRouter.run(): {e}")
            self._running = False
        finally:
            logger.info("<<< ZmqRouter.run() FINALIZADO (bloco finally).")
            for socket in self.sockets.values():
                if not socket.closed:
                    socket.close()
            for socket in self.data_sockets.values():
                if not socket.closed:
                    socket.close()
            logger.info("Sockets ZMQ fechados.")

# Versão 1.0.9.a - Envio 6