# core/config_manager.py
import configparser
import os
import logging

logger = logging.getLogger(__name__)

class ConfigManager:
    def __init__(self, config_file="config.ini"):
        self.config_file = config_file
        self.config = configparser.ConfigParser(interpolation=None) # interpolation=None para evitar problemas com '%'
        self.load_config()

    def load_config(self):
        """Carrega o config.ini ou cria um novo se não existir."""
        if not os.path.exists(self.config_file):
            logger.warning(f"Arquivo de configuração '{self.config_file}' não encontrado. Criando um com valores padrão.")
            self.create_default_config()
        try:
            self.config.read(self.config_file, encoding='utf-8')
            logger.info(f"Arquivo de configuração '{self.config_file}' carregado.")
        except configparser.Error as e:
            logger.error(f"Erro ao ler o arquivo de configuração '{self.config_file}': {e}")
            # Opcional: Criar um padrão em caso de erro de leitura?
            # self.create_default_config()

    def create_default_config(self):
        """Cria um config.ini com valores padrão."""
        try:
            self.config['General'] = {
                'base_mt5_path': 'C:/Program Files/MetaTrader 5', # Exemplo, ajustar conforme necessário
                'brokers_file': 'brokers.json',
                'log_level': 'INFO', # Movido para General por conveniência ou manter em Logging
                 # Adicione outros padrões gerais se necessário
            }
            self.config['ZMQ'] = {
                'host': '127.0.0.1',
                'port': '5555',
                'heartbeat_interval_s': '5', # Intervalo de heartbeat do EA
            }
            self.config['GUI'] = {
                 # Adicione configurações da GUI se necessário
            }
            # Mantendo Logging separado se preferir
            # self.config['Logging'] = {
            #     'log_level': 'INFO'
            # }
            self.save_config()
            logger.info(f"Arquivo de configuração padrão '{self.config_file}' criado.")
        except IOError as e:
             logger.error(f"Erro ao criar o arquivo de configuração padrão '{self.config_file}': {e}")


    def save_config(self):
        """Salva as alterações no config.ini."""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)
            logger.debug(f"Configurações salvas em '{self.config_file}'.")
        except IOError as e:
            logger.error(f"Erro ao salvar o arquivo de configuração '{self.config_file}': {e}")

    def get(self, section, key, fallback=None):
        """Obtém um valor do config como string, com fallback opcional."""
        return self.config.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=None):
        """Obtém um valor do config como inteiro, com fallback opcional."""
        value_str = self.config.get(section, key, fallback=None)
        if value_str is None:
            logger.debug(f"Chave '{key}' não encontrada na seção '{section}'. Usando fallback int: {fallback}")
            return fallback
        try:
            return int(value_str)
        except (ValueError, TypeError):
            logger.warning(f"Falha ao converter '{value_str}' para int na seção '{section}', chave '{key}'. Usando fallback: {fallback}")
            return fallback

    def getfloat(self, section, key, fallback=None):
        """Obtém um valor do config como float, com fallback opcional."""
        value_str = self.config.get(section, key, fallback=None)
        if value_str is None:
            logger.debug(f"Chave '{key}' não encontrada na seção '{section}'. Usando fallback float: {fallback}")
            return fallback
        try:
            # Substituir vírgula por ponto, se necessário, para consistência
            return float(value_str.replace(',', '.'))
        except (ValueError, TypeError):
            logger.warning(f"Falha ao converter '{value_str}' para float na seção '{section}', chave '{key}'. Usando fallback: {fallback}")
            return fallback

    def getboolean(self, section, key, fallback=None):
        """Obtém um valor do config como booleano, com fallback opcional."""
        # O getboolean do configparser já tem um fallback em algumas versões,
        # mas para consistência e robustez, faremos manualmente.
        value_str = self.config.get(section, key, fallback=None)
        if value_str is None:
            logger.debug(f"Chave '{key}' não encontrada na seção '{section}'. Usando fallback bool: {fallback}")
            return fallback

        normalized_value = value_str.strip().lower()
        if normalized_value in ('true', 'yes', 'on', '1'):
            return True
        elif normalized_value in ('false', 'no', 'off', '0'):
            return False
        else:
            # Se não for um booleano reconhecido, usa o fallback
            logger.warning(f"Valor '{value_str}' não reconhecido como booleano na seção '{section}', chave '{key}'. Usando fallback: {fallback}")
            return fallback


    def set(self, section, key, value):
        """Define um valor no config e salva."""
        if not self.config.has_section(section):
            self.config.add_section(section)
            logger.info(f"Seção '{section}' criada no arquivo de configuração.")
        self.config.set(section, key, str(value))
        logger.debug(f"Configuração definida: [{section}] {key} = {value}")
        self.save_config() # Salva imediatamente após cada 'set'

# Versão 1.0.0 - envio 18 - Cria diretório das instâncias portáteis do MT5