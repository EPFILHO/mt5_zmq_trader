# core/broker_manager.py
# Versão 1.0.9.j - envio 4
# Ajustes:
# - Adicionado suporte ao sinal brokers_updated para notificar mudanças na lista de corretoras
#   ou status de conexão, garantindo que as abas no BoletaTraderGui sejam reordenadas alfabeticamente.
# - Transformado BrokerManager em QObject para suportar sinais do Qt.
# - Emitido brokers_updated nos métodos add_broker, remove_broker, modify_broker, connect_broker,
#   e disconnect_broker.
# - Mantidas todas as funcionalidades existentes (gerenciamento de instâncias MT5, ZMQ, etc.).
# - Alterações mínimas para preservar o comportamento original.

import json
import os
import shutil
import logging
import subprocess
import sys
import configparser
import asyncio
from PySide6.QtCore import QObject, Signal  # Adicionado para suportar sinais

logger = logging.getLogger(__name__)

class BrokerManager(QObject):
    # Sinal para notificar mudanças na lista de corretoras ou status de conexão
    brokers_updated = Signal()

    # Bloco 1 - Inicialização da Classe BrokerManager
    # Objetivo: Inicializar o gerenciador de corretoras, carregar configurações e preparar o ambiente.
    def __init__(self, config, base_mt5_path, root_path, zmq_router):
        """
        Inicializa o gerenciador de corretoras.

        Args:
            config (ConfigManager): Instância do gerenciador de configuração.
            base_mt5_path (str): Caminho base do MT5 para criar instâncias portáteis.
            root_path (str): Caminho da raiz do projeto.
            zmq_router (ZmqRouter): Instância do roteador ZMQ para gerenciar sockets.
        """
        super().__init__()  # Inicializa QObject
        logger.debug("Bloco 1 - BrokerManager.__init__ chamado.")
        self.brokers_file = config.get('General', 'brokers_file', fallback='brokers.json')
        self.base_mt5_path = base_mt5_path
        self.root_path = root_path
        self.instances_dir = os.path.join(self.root_path, ".mt5_instances")
        self.brokers = self.load_brokers()
        self.connected_brokers = {}  # Dicionário para rastrear o status de conexão
        self.mt5_processes = {}  # Dicionário para armazenar os processos MT5
        self.zmq_router = zmq_router
        logger.debug("Bloco 1 - BrokerManager.__init__ concluído.")

    # Bloco 2 - Gerenciamento de Arquivos de Corretoras (load_brokers, save_brokers)
    # Objetivo: Carregar e salvar os dados das corretoras em um arquivo JSON.
    def load_brokers(self):
        """Carrega as corretoras do arquivo JSON.

        Returns:
            dict: Dicionário com os dados das corretoras ou vazio se o arquivo não existir.
        """
        try:
            if os.path.exists(self.brokers_file):
                with open(self.brokers_file, 'r') as f:
                    brokers = json.load(f)
                    self.connected_brokers = {key: False for key in brokers}
                    logger.info(f"Bloco 2 - Corretoras carregadas do arquivo: {len(brokers)}.")
                    return brokers
            logger.info("Bloco 2 - Arquivo de corretoras não encontrado ou vazio. Retornando dicionário vazio.")
            return {}
        except Exception as e:
            logger.error(f"Bloco 2 - Erro ao carregar corretoras: {str(e)}")
            return {}

    def save_brokers(self):
        """Salva as corretoras no arquivo JSON."""
        try:
            with open(self.brokers_file, 'w') as f:
                json.dump(self.brokers, f, indent=4)
            logger.info("Bloco 2 - Corretoras salvas no arquivo.")
        except Exception as e:
            logger.error(f"Bloco 2 - Erro ao salvar corretoras: {str(e)}")

    # Bloco 3 - Operações CRUD de Corretoras (add_broker, remove_broker, modify_broker)
    # Objetivo: Adicionar, remover e modificar registros de corretoras, incluindo a criação/remoção de instâncias MT5 portáteis.
    def add_broker(self, name, broker_name, login, password, server,
                   admin_port, data_port, live_port, str_port, trade_port,
                   client="", mode="", type_=""):
        """Adiciona uma nova corretora e cria sua instância portátil.

        Args:
            name (str): Nome do titular.
            broker_name (str): Nome da corretora.
            login (str): Login da conta.
            password (str): Senha da conta.
            server (str): Servidor da corretora.
            admin_port (int): Porta ZMQ para administração.
            data_port (int): Porta ZMQ para dados.
            live_port (int): Porta ZMQ para dados em tempo real.
            str_port (int): Porta ZMQ para streaming.
            trade_port (int): Porta ZMQ para trading.
            client (str): Nome do cliente.
            mode (str): Modo de operação (Hedge/Netting).
            type_ (str): Tipo de conta (Demo/Real).

        Returns:
            str: Chave da corretora adicionada ou None se falhar.
        """
        key = f"{broker_name.upper()}-{login}"
        if key in self.brokers:
            logger.error(f"Bloco 3 - Corretora {key} já existe.")
            return None

        instance_path = self.setup_portable_instance(key)
        if not instance_path:
            return None

        self.brokers[key] = {
            "name": name,
            "client": client,
            "broker_name": broker_name,
            "login": login,
            "password": password,
            "server": server,
            "type": type_,
            "mode": mode,
            "admin_port": admin_port,
            "data_port": data_port,
            "live_port": live_port,
            "str_port": str_port,
            "trade_port": trade_port
        }
        self.save_brokers()
        self.connected_brokers[key] = False
        self.create_mt5_config(key, admin_port, data_port, live_port, str_port, trade_port)
        logger.info(f"Bloco 3 - Corretora {key} adicionada com sucesso.")
        self.brokers_updated.emit()  # Emitir sinal
        return key

    def remove_broker(self, key):
        """Remove uma corretora e seu diretório MT5.

        Args:
            key (str): Chave da corretora (ex.: "BROKER-LOGIN").

        Returns:
            bool: True se removida, False se não encontrada.
        """
        if key not in self.brokers:
            logger.error(f"Bloco 3 - Corretora {key} não encontrada.")
            return False

        if self.is_connected(key):
            logger.warning(f"Bloco 3 - Corretora {key} está conectada. Desconectando antes de remover.")
            self.disconnect_broker(key)

        del self.brokers[key]
        self.save_brokers()
        if key in self.connected_brokers:
            del self.connected_brokers[key]
        instance_path = os.path.join(self.instances_dir, key)
        if os.path.exists(instance_path):
            shutil.rmtree(instance_path, ignore_errors=True)
            logger.info(f"Bloco 3 - Diretório MT5 de {key} excluído: {instance_path}")
        logger.info(f"Bloco 3 - Corretora {key} removida com sucesso.")
        self.brokers_updated.emit()  # Emitir sinal
        return True

    def modify_broker(self, old_key, name, broker_name, login, password, server,
                      admin_port, data_port, live_port, str_port, trade_port,
                      client="", mode="", type_=""):
        """Modifica uma corretora existente.

        Args:
            old_key (str): Chave atual da corretora.
            name (str): Novo nome do titular.
            broker_name (str): Nome da corretora.
            login (str): Novo login.
            password (str): Nova senha.
            server (str): Novo servidor.
            admin_port (int): Nova porta ZMQ para administração.
            data_port (int): Nova porta ZMQ para dados.
            live_port (int): Nova porta ZMQ para dados em tempo real.
            str_port (int): Nova porta ZMQ para streaming.
            trade_port (int): Nova porta ZMQ para trading.
            client (str): Novo nome do cliente.
            mode (str): Novo modo (Hedge/Netting).
            type_ (str): Novo tipo (Demo/Real).

        Returns:
            str: Nova chave da corretora ou None se falhar.
        """
        if old_key not in self.brokers:
            logger.error(f"Bloco 3 - Corretora {old_key} não encontrada.")
            return None

        if self.is_connected(old_key):
            logger.warning(f"Bloco 3 - Corretora {old_key} está conectada. Desconectando antes de modificar.")
            self.disconnect_broker(old_key)

        old_data = self.brokers.pop(old_key)
        if broker_name is None:
            broker_name = old_data.get("broker_name", old_key.split("-")[0])
        new_key = f"{broker_name.upper()}-{login}"
        if new_key != old_key and new_key in self.brokers:
            logger.error(f"Bloco 3 - Já existe uma corretora com a chave {new_key}.")
            self.brokers[old_key] = old_data
            return None

        if new_key != old_key:
            old_instance_path = os.path.join(self.instances_dir, old_key)
            if os.path.exists(old_instance_path):
                shutil.rmtree(old_instance_path, ignore_errors=True)
            self.setup_portable_instance(new_key)

        self.brokers[new_key] = {
            "name": name,
            "client": client if client != "" else old_data.get("client", ""),
            "broker_name": broker_name,
            "login": login,
            "password": password,
            "server": server,
            "type": type_ if type_ != "" else old_data.get("type", ""),
            "mode": mode if mode != "" else old_data.get("mode", ""),
            "admin_port": admin_port,
            "data_port": data_port,
            "live_port": live_port,
            "str_port": str_port,
            "trade_port": trade_port
        }
        self.save_brokers()
        if old_key in self.connected_brokers:
            self.connected_brokers[new_key] = self.connected_brokers.pop(old_key)
        else:
            self.connected_brokers[new_key] = False
        self.create_mt5_config(new_key, admin_port, data_port, live_port, str_port, trade_port)
        logger.info(f"Bloco 3 - Corretora {old_key} modificada para {new_key}.")
        self.brokers_updated.emit()  # Emitir sinal
        return new_key

    # Bloco 4 - Gerenciamento de Instâncias MT5 Portáteis (setup_portable_instance, copy_dlls, copy_expert, create_mt5_config)
    # Objetivo: Criar, configurar e gerenciar os diretórios das instâncias portáteis do MT5 para cada corretora.
    def setup_portable_instance(self, key):
        """Cria uma instância portátil copiando o diretório base do MT5.

        Args:
            key (str): Chave da corretora (ex.: "BROKER-LOGIN").

        Returns:
            str: Caminho do executável MT5 ou None se falhar.
        """
        instance_path = os.path.join(self.instances_dir, key)
        executable = os.path.join(instance_path, "terminal64.exe")
        if not os.path.exists(instance_path):
            try:
                os.makedirs(self.instances_dir, exist_ok=True)
                shutil.copytree(self.base_mt5_path, instance_path)
                self.copy_dlls(instance_path)
                self.copy_expert(instance_path)
                import win32api, win32con
                win32api.SetFileAttributes(instance_path, win32con.FILE_ATTRIBUTE_HIDDEN)
                logger.info(f"Bloco 4 - Instância MT5 criada para {key} em {instance_path}")
            except Exception as e:
                logger.error(f"Bloco 4 - Erro ao criar instância para {key}: {str(e)}")
                return None
        return executable

    def copy_dlls(self, instance_path):
        r"""Copia as DLLs para a pasta MQL5\Libraries da instância."""
        source_dll_path = os.path.join(self.root_path, "dlls")
        dest_dll_path = os.path.join(instance_path, "MQL5", "Libraries")
        if not os.path.exists(dest_dll_path):
            os.makedirs(dest_dll_path)
        try:
            for filename in os.listdir(source_dll_path):
                if filename.endswith(".dll"):
                    source_file = os.path.join(source_dll_path, filename)
                    dest_file = os.path.join(dest_dll_path, filename)
                    shutil.copy2(source_file, dest_file)
                    logger.debug(f"Bloco 4 - DLL copiada: {filename} para {dest_file}")
        except Exception as e:
            logger.error(f"Bloco 4 - Erro ao copiar DLLs: {str(e)}")

    def copy_expert(self, instance_path):
        r"""Copia o Expert Advisor para a pasta MQL5\Experts da instância."""
        source_expert_path = os.path.join(self.root_path, "mt5_ea", "ZmqTraderBridge.ex5")
        dest_expert_path = os.path.join(instance_path, "MQL5", "Experts")
        if not os.path.exists(dest_expert_path):
            os.makedirs(dest_expert_path)
        try:
            shutil.copy2(source_expert_path, dest_expert_path)
            logger.debug(f"Bloco 4 - Expert Advisor copiado para {dest_expert_path}")
        except Exception as e:
            logger.error(f"Bloco 4 - Erro ao copiar Expert Advisor: {str(e)}")

    def create_mt5_config(self, key, admin_port, data_port, live_port, str_port, trade_port):
        """
        Cria o arquivo config.ini na pasta do MT5 com as portas ZMQ,
        sem linhas em branco e sem espaços em torno do '='.
        """
        instance_path = os.path.join(self.instances_dir, key)
        config_file_path = os.path.join(instance_path, "MQL5", "Files", "config.ini")
        lines = [
            "[ZMQ]",
            f"BrokerKey={key}",
            "[Ports]",
            f"AdminPort={admin_port}",
            f"DataPort={data_port}",
            f"LivePort={live_port}",
            f"StrPort={str_port}",
            f"TradePort={trade_port}"
        ]
        content = "\n".join(lines)
        try:
            os.makedirs(os.path.dirname(config_file_path), exist_ok=True)
            with open(config_file_path, 'w', encoding='utf-8') as configfile:
                configfile.write(content)
            logger.info(f"Bloco 4 - Arquivo config.ini criado em {config_file_path}")
        except Exception as e:
            logger.error(f"Bloco 4 - Erro ao criar o arquivo config.ini: {str(e)}")

    # Bloco 5 - Gerenciamento de Conexão de Corretoras (connect_broker, disconnect_broker, is_connected, get_connected_brokers)
    # Objetivo: Iniciar e parar os processos MT5 e gerenciar o status de conexão das corretoras,
    # incluindo a interação com o ZmqRouter para as portas ZMQ.
    def get_brokers(self):
        """Retorna o dicionário de corretoras.

        Returns:
            dict: Todas as corretoras carregadas.
        """
        return self.brokers

    def connect_broker(self, key):
        """Conecta uma corretora.

        Args:
            key (str): Chave da corretora (ex.: "BROKER-LOGIN").

        Returns:
            bool: True se a conexão foi bem-sucedida, False caso contrário.
        """
        if key not in self.brokers:
            logger.error(f"Bloco 5 - Corretora {key} não encontrada.")
            return False

        process_is_running = False
        if key in self.mt5_processes and self.mt5_processes[key].poll() is None:
            process_is_running = True

        if process_is_running:
            logger.warning(f"Bloco 5 - MT5 já está em execução para a corretora {key}.")
            if self.zmq_router:
                broker_config = self.brokers[key]
                asyncio.create_task(self.zmq_router.connect_broker_sockets(key, broker_config))
                logger.info(f"Bloco 5 - Tentando reconectar sockets ZMQ para {key}.")
            self.connected_brokers[key] = True
            self.brokers_updated.emit()  # Emitir sinal
            return True

        instance_path = os.path.join(self.instances_dir, key, "terminal64.exe")
        if not os.path.exists(instance_path):
            logger.error(f"Bloco 5 - Instância do MT5 não encontrada para a corretora {key}.")
            return False

        try:
            logger.info(f"Bloco 5 - Iniciando MT5 para a corretora {key} (minimizado)...")
            if sys.platform.startswith("win"):
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 6  # SW_MINIMIZE
                process = subprocess.Popen(
                    [instance_path, "/portable"],
                    cwd=os.path.dirname(instance_path),
                    startupinfo=si
                )
            else:
                process = subprocess.Popen(
                    [instance_path, "/portable"],
                    cwd=os.path.dirname(instance_path)
                )
            self.mt5_processes[key] = process
            self.connected_brokers[key] = True
            logger.info(f"Bloco 5 - MT5 iniciado com sucesso para a corretora {key}.")
            if self.zmq_router:
                broker_config = self.brokers[key]
                asyncio.create_task(self.zmq_router.connect_broker_sockets(key, broker_config))
                logger.info(f"Bloco 5 - Solicitado ao ZmqRouter para conectar sockets para {key}.")
            else:
                logger.warning(f"Bloco 5 - ZmqRouter não disponível para conectar sockets para {key}.")
            self.brokers_updated.emit()  # Emitir sinal
            return True
        except Exception as e:
            logger.error(f"Bloco 5 - Erro ao iniciar MT5 para a corretora {key}: {e}")
            return False

    def disconnect_broker(self, key):
        """Desconecta uma corretora.

        Args:
            key (str): Chave da corretora (ex.: "BROKER-LOGIN").

        Returns:
            bool: True se a desconexão foi bem-sucedida, False caso contrário.
        """
        if key not in self.brokers:
            logger.error(f"Bloco 5 - Corretora {key} não encontrada.")
            return False

        if self.zmq_router:
            asyncio.create_task(self.zmq_router.disconnect_broker_sockets(key))
            logger.info(f"Bloco 5 - Solicitado ao ZmqRouter para desconectar sockets para {key}.")
        else:
            logger.warning(f"Bloco 5 - ZmqRouter não disponível para desconectar sockets para {key}.")

        process_to_terminate = None
        if key in self.mt5_processes:
            process_obj = self.mt5_processes[key]
            poll_result = process_obj.poll()
            logger.debug(f"Bloco 5 - Verificando processo MT5 para {key}. Resultado de poll(): {poll_result}")
            if poll_result is None:
                process_to_terminate = process_obj
                logger.info(f"Bloco 5 - Processo MT5 para {key} está ativo (poll() retornou None). Tentando terminá-lo.")
            else:
                logger.warning(f"Bloco 5 - Processo MT5 para {key} já terminou (exit code: {poll_result}) ou não estava em execução ativa. Limpando registro interno.")
                del self.mt5_processes[key]
        else:
            logger.warning(f"Bloco 5 - Processo MT5 para {key} não encontrado no rastreamento (já limpo ou nunca iniciado?).")

        if process_to_terminate:
            try:
                logger.info(f"Bloco 5 - Parando MT5 para a corretora {key} (via terminate())...")
                process_to_terminate.terminate()
                process_to_terminate.wait(timeout=5)
                del self.mt5_processes[key]
                logger.info(f"Bloco 5 - MT5 parado com sucesso para a corretora {key}.")
            except subprocess.TimeoutExpired:
                logger.warning(f"Bloco 5 - Timeout ao esperar MT5 para {key} terminar. Matando o processo (via kill())...")
                process_to_terminate.kill()
                del self.mt5_processes[key]
            except Exception as e:
                logger.error(f"Bloco 5 - Erro ao parar MT5 para a corretora {key}: {e}")
                self.connected_brokers[key] = False
                self.brokers_updated.emit()  # Emitir sinal mesmo em caso de erro
                return False

        self.connected_brokers[key] = False
        self.brokers_updated.emit()  # Emitir sinal
        return True

    def is_connected(self, key):
        """Verifica se uma corretora está conectada.

        Args:
            key (str): Chave da corretora (ex.: "BROKER-LOGIN").

        Returns:
            bool: True se a corretora estiver conectada, False caso contrário.
        """
        return self.connected_brokers.get(key, False)

    def get_connected_brokers(self):
        """Retorna uma lista das corretoras conectadas.

        Returns:
            list: Lista das chaves das corretoras conectadas.
        """
        return [key for key, connected in self.connected_brokers.items() if connected]

# core/broker_manager.py
# Versão 1.0.9.j - envio 4