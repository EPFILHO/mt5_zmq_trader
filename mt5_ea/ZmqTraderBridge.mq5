//+------------------------------------------------------------------+
//|                                              ZmqTraderBridge.mq5 |
//|                        MQL5 <-> Python ZeroMQ Bridge for Trading |
//|                                              EPFilho / Empresa   |
//+------------------------------------------------------------------+
#property copyright "EPFilho"
#property link      "epfilho73@gmail.com"
#property version   "1.11"
#property strict

#include <Zmq/Zmq.mqh>
#include <Json.mqh>
#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| Bloco 1 - Configuração e Conexão ZMQ                            |
//| - Contém variáveis globais, parâmetros de entrada, leitura de   |
//|   config.ini, conexão com sockets ZMQ, e funções de envio/      |
//|   recebimento de mensagens.                                     |
//| - Este bloco é a base para comunicação com o Python via ZMQ.    |
//| - Observação: Futuro suporte para streaming de ticks pode       |
//|   incluir um novo socket exclusivo (tick_socket).               |
//+------------------------------------------------------------------+

//--- Parâmetros configuráveis
input int    InpTimerIntervalMs  = 200;                    // Intervalo do timer (ms)
input bool   InpDebugLog         = true;                   // Ativar logs

//--- Variáveis globais
Context context;
Socket  admin_socket(context, ZMQ_DEALER);
Socket  data_socket(context, ZMQ_DEALER);
Socket  trade_socket(context, ZMQ_SUB);
Socket  live_socket(context, ZMQ_PUB);
Socket  stream_socket(context, ZMQ_PUB);
bool    g_is_connected = false;
datetime g_last_ping_time = 0;
long    g_ping_latency = 0;
CTrade  trade;

//--- Estruturas para streaming
struct IndicatorConfig {
   string type;
   int    period;
};

struct StreamRequest {
   string symbol;
   ENUM_TIMEFRAMES timeframe;
   string request_id;
   datetime last_sent_time;
   IndicatorConfig indicators[];
};

StreamRequest g_stream_requests[];
bool g_streaming_active = false;

//--- Variáveis para config.ini
string g_brokerKey = "";
int    g_adminPort = 0;
int    g_dataPort = 0;
int    g_tradePort = 0;
int    g_livePort = 0;
int    g_strPort = 0;

//+------------------------------------------------------------------+
//| Função auxiliar para trim de string                              |
//+------------------------------------------------------------------+
string TrimString(string s)
{
   StringTrimLeft(s);
   StringTrimRight(s);
   return s;
}

//+------------------------------------------------------------------+
//| Lê as configurações do arquivo config.ini                        |
//+------------------------------------------------------------------+
bool ReadConfigFile(string &brokerKey, int &adminPort, int &dataPort, int &tradePort, int &livePort, int &strPort)
{
   int file_handle = FileOpen("config.ini", FILE_READ|FILE_ANSI|FILE_TXT);
   if(file_handle == INVALID_HANDLE)
   {
      int error_code = GetLastError();
      Print("Erro ao abrir o arquivo config.ini. Erro code = ", IntegerToString(error_code));
      string file_path = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\config.ini";
      Print("Caminho esperado do arquivo: ", file_path);
      return false;
   }
   string linha;
   int posicaoIgual;
   while(!FileIsEnding(file_handle))
   {
      linha = FileReadString(file_handle);
      if(StringFind(linha, "[ZMQ]") >= 0)
      {
         while(!FileIsEnding(file_handle))
         {
            linha = FileReadString(file_handle);
            posicaoIgual = StringFind(linha, "=");
            if(posicaoIgual > 0)
            {
               string chave = TrimString(StringSubstr(linha, 0, posicaoIgual));
               string valor = TrimString(StringSubstr(linha, posicaoIgual + 1));
               if(chave == "BrokerKey") brokerKey = valor;
            }
            if(StringFind(linha, "[") >= 0) break;
         }
      }
      if(StringFind(linha, "[Ports]") >= 0)
      {
         while(!FileIsEnding(file_handle))
         {
            linha = FileReadString(file_handle);
            posicaoIgual = StringFind(linha, "=");
            if(posicaoIgual > 0)
            {
               string chave = TrimString(StringSubstr(linha, 0, posicaoIgual));
               string valor = TrimString(StringSubstr(linha, posicaoIgual + 1));
               if(chave == "AdminPort") adminPort = (int)StringToInteger(valor);
               else if(chave == "DataPort") dataPort = (int)StringToInteger(valor);
               else if(chave == "TradePort") tradePort = (int)StringToInteger(valor);
               else if(chave == "LivePort") livePort = (int)StringToInteger(valor);
               else if(chave == "StrPort") strPort = (int)StringToInteger(valor);
            }
         }
      }
   }
   FileClose(file_handle);
   if(InpDebugLog)
   {
      PrintFormat("Configurações lidas do arquivo config.ini:");
      PrintFormat("  BrokerKey: %s", brokerKey);
      PrintFormat("  AdminPort: %d", adminPort);
      PrintFormat("  DataPort: %d", dataPort);
      PrintFormat("  TradePort: %d", tradePort);
      PrintFormat("  LivePort: %d", livePort);
      PrintFormat("  StrPort: %d", strPort);
   }
   return true;
}

//+------------------------------------------------------------------+
//| Valida portas para evitar conflitos                              |
//+------------------------------------------------------------------+
bool ValidatePorts()
{
   if(g_adminPort == g_dataPort || g_adminPort == g_tradePort || g_adminPort == g_livePort || g_adminPort == g_strPort ||
      g_dataPort == g_tradePort || g_dataPort == g_livePort || g_dataPort == g_strPort ||
      g_tradePort == g_livePort || g_tradePort == g_strPort || g_livePort == g_strPort)
   {
      Print("ZmqTraderBridge: Erro: Portas devem ser únicas");
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| Serializa JSON de forma robusta                                 |
//+------------------------------------------------------------------+
string RobustJsonSerialize(JSONNode &json_message)
{
   string msg = json_message.Serialize();
   int real_len = StringLen(msg);
   if(real_len >= 255)
   {
      msg = msg + msg;
      msg = StringSubstr(msg, 0, real_len);
   }
   if(real_len == 0 || msg[real_len-1] != '}')
   {
      Print("ZmqTraderBridge WARN: JSON não termina com '}'. Corrigindo.");
      msg = StringSubstr(msg, 0, StringFind(msg, "}") + 1);
      if(StringFind(msg, "}") == -1)
      {
         msg += "}";
      }
   }
   return msg;
}

//+------------------------------------------------------------------+
//| Enviar mensagem JSON por socket específico                      |
//+------------------------------------------------------------------+
bool SendJsonMessage(JSONNode &json_message, Socket &target_socket, string socket_name="Admin")
{
   json_message["broker_key"] = g_brokerKey;
   if(!g_is_connected)
   {
      Print("ZmqTraderBridge ERROR: Tentativa de envio sem conexão em ", socket_name);
      return false;
   }
   string message_str = RobustJsonSerialize(json_message);
   if(InpDebugLog)
      Print("ZmqTraderBridge DEBUG: Enviando em ", socket_name, ": ", message_str);

   ZmqMsg msg(message_str);
   bool sent = target_socket.send(msg);
   if(!sent)
   {
      PrintFormat("ZMQ Bridge ERROR: Falha ao enviar em %s. GetLastError(): %d", socket_name, GetLastError());
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| Mensagens de Sistema                                            |
//+------------------------------------------------------------------+
bool SendRegisterMessage(Socket &response_socket, string socket_name)
{
   JSONNode message;
   message["type"] = "SYSTEM";
   message["event"] = "REGISTER";
   message["mt5_build"] = (long)TerminalInfoInteger(TERMINAL_BUILD);
   message["timestamp_mql"] = (long)TimeCurrent();
   PrintFormat("ZmqTraderBridge: Enviando REGISTER para %s", g_brokerKey);
   return SendJsonMessage(message, response_socket, socket_name);
}

bool SendUnregisterMessage(Socket &response_socket, string socket_name)
{
   if(!g_is_connected) return false;
   JSONNode message;
   message["type"] = "SYSTEM";
   message["event"] = "UNREGISTER";
   message["timestamp_mql"] = (long)TimeCurrent();
   PrintFormat("ZmqTraderBridge: Enviando UNREGISTER para %s", g_brokerKey);
   return SendJsonMessage(message, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Resposta de erro padrão                                         |
//+------------------------------------------------------------------+
bool SendErrorResponse(const string request_id, const string error_message, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "ERROR";
   response["error_message"] = error_message;
   return SendJsonMessage(response, response_socket, socket_name);
}
//+------------------------------------------------------------------+
//| Bloco 2 - Comandos Administrativos                              |
//| - Contém handlers para comandos relacionados a informações da   |
//|   conta, broker, status do terminal, posições, ordens e histórico.|
//| - Este bloco processa solicitações administrativas enviadas pelo |
//|   Python, retornando dados gerais sobre a conta e o mercado.    |
//+------------------------------------------------------------------+


//+------------------------------------------------------------------+
//| Comando PING                                                    |
//+------------------------------------------------------------------+
void HandlePingCommand(const string request_id, JSONNode *payload_node_ptr, Socket &response_socket, string socket_name)
{
   if(InpDebugLog) Print("ZMQ Bridge: Recebido comando PING.");
   long original_timestamp = 0;
   if(CheckPointer(payload_node_ptr) != POINTER_INVALID)
   {
      JSONNode *ts_node_ptr = (*payload_node_ptr)["timestamp"];
      if(CheckPointer(ts_node_ptr) != POINTER_INVALID)
         original_timestamp = ts_node_ptr.ToInteger();
   }
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["original_timestamp"] = original_timestamp;
   response["pong_timestamp_mql"] = (long)TimeCurrent();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_STATUS_INFO                                         |
//+------------------------------------------------------------------+
void HandleGetStatusInfoCommand(const string request_id, JSONNode *payload_node_ptr, Socket &response_socket, string socket_name)
{
   if(InpDebugLog) Print("ZMQ Bridge: Recebido comando GET_STATUS_INFO.");
   long original_timestamp = 0;
   if(CheckPointer(payload_node_ptr) != POINTER_INVALID)
   {
      JSONNode *ts_node_ptr = (*payload_node_ptr)["timestamp"];
      if(CheckPointer(ts_node_ptr) != POINTER_INVALID)
         original_timestamp = ts_node_ptr.ToInteger();
   }
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["trade_allowed"] = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
   response["balance"] = AccountInfoDouble(ACCOUNT_BALANCE);
   response["original_timestamp"] = original_timestamp;
   response["pong_timestamp_mql"] = (long)TimeCurrent();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_BROKER_INFO                                         |
//+------------------------------------------------------------------+
void HandleGetBrokerInfoCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["company"] = AccountInfoString(ACCOUNT_COMPANY);
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_BROKER_SERVER                                       |
//+------------------------------------------------------------------+
void HandleGetBrokerServerCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["server"] = AccountInfoString(ACCOUNT_SERVER);
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_BROKER_PATH                                         |
//+------------------------------------------------------------------+
void HandleGetBrokerPathCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["mt5_path"] = TerminalInfoString(TERMINAL_PATH);
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_ACCOUNT_INFO                                        |
//+------------------------------------------------------------------+
void HandleGetAccountInfoCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["login"] = (long)AccountInfoInteger(ACCOUNT_LOGIN);
   response["name"] = AccountInfoString(ACCOUNT_NAME);
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_ACCOUNT_BALANCE                                     |
//+------------------------------------------------------------------+
void HandleGetAccountBalanceCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["balance"] = AccountInfoDouble(ACCOUNT_BALANCE);
   response["equity"] = AccountInfoDouble(ACCOUNT_EQUITY);
   response["currency"] = AccountInfoString(ACCOUNT_CURRENCY);
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_ACCOUNT_LEVERAGE                                    |
//+------------------------------------------------------------------+
void HandleGetAccountLeverageCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["leverage"] = (int)AccountInfoInteger(ACCOUNT_LEVERAGE);
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_ACCOUNT_FLAGS                                       |
//+------------------------------------------------------------------+
void HandleGetAccountFlagsCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["trade_allowed"] = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
   response["expert_enabled"] = (bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT);
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_ACCOUNT_MARGIN                                      |
//+------------------------------------------------------------------+
void HandleGetAccountMarginCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["margin"] = AccountInfoDouble(ACCOUNT_MARGIN);
   response["free_margin"] = AccountInfoDouble(ACCOUNT_FREEMARGIN);
   response["margin_level"] = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_ACCOUNT_STATE                                       |
//+------------------------------------------------------------------+
void HandleGetAccountStateCommand(const string request_id, Socket &response_socket, string socket_name)
{
   int trade_mode = (int)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   string state = trade_mode == ACCOUNT_TRADE_MODE_DEMO ? "demo" :
                  trade_mode == ACCOUNT_TRADE_MODE_CONTEST ? "contest" :
                  trade_mode == ACCOUNT_TRADE_MODE_REAL ? "real" : "unknown";
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["account_state"] = state;
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_TIME_SERVER                                         |
//+------------------------------------------------------------------+
void HandleGetTimeServerCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["time_server"] = (long)TimeTradeServer();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando POSITIONS                                               |
//+------------------------------------------------------------------+
void HandleGetPositionsCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";

   JSONNode positions_array;
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
      {
         JSONNode pos;
         pos["ticket"] = (long)ticket;
         pos["symbol"] = PositionGetString(POSITION_SYMBOL);
         pos["type"] = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL";
         pos["volume"] = PositionGetDouble(POSITION_VOLUME);
         pos["price_open"] = PositionGetDouble(POSITION_PRICE_OPEN);
         pos["sl"] = PositionGetDouble(POSITION_SL);
         pos["tp"] = PositionGetDouble(POSITION_TP);
         pos["profit"] = PositionGetDouble(POSITION_PROFIT);
         positions_array.Add(pos);
      }
   }
   response["positions"] = positions_array;
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando ORDERS                                                  |
//+------------------------------------------------------------------+
void HandleGetOrdersCommand(const string request_id, Socket &response_socket, string socket_name)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";

   JSONNode orders_array;
   for(int i = 0; i < OrdersTotal(); i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(OrderSelect(ticket))
      {
         JSONNode ord;
         ord["ticket"] = (long)ticket;
         ord["symbol"] = OrderGetString(ORDER_SYMBOL);
         ord["type"] = EnumToString((ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE));
         ord["volume"] = OrderGetDouble(ORDER_VOLUME_CURRENT);
         ord["price"] = OrderGetDouble(ORDER_PRICE_OPEN);
         ord["sl"] = OrderGetDouble(ORDER_SL);
         ord["tp"] = OrderGetDouble(ORDER_TP);
         orders_array.Add(ord);
      }
   }
   response["orders"] = orders_array;
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando HISTORY_DATA                                            |
//+------------------------------------------------------------------+
void HandleGetHistoryDataCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   string timeframe = payload["timeframe"].ToString();
   long start_time = payload["start_time"].ToInteger();
   long end_time = payload["end_time"].ToInteger();

   ENUM_TIMEFRAMES tf = StringToTimeframe(timeframe);
   if(tf == PERIOD_CURRENT) tf = (ENUM_TIMEFRAMES)_Period;

   MqlRates rates[];
   int copied = CopyRates(symbol, tf, (datetime)start_time, (datetime)end_time, rates);
   if(copied <= 0)
   {
      SendErrorResponse(request_id, "Falha ao obter dados históricos", response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";

   JSONNode rates_array;
   for(int i = 0; i < copied; i++)
   {
      JSONNode rate;
      rate["time"] = (long)rates[i].time;
      rate["open"] = rates[i].open;
      rate["high"] = rates[i].high;
      rate["low"] = rates[i].low;
      rate["close"] = rates[i].close;
      rate["volume"] = (long)rates[i].tick_volume;
      rates_array.Add(rate);
   }
   response["rates"] = rates_array;
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando HISTORY_TRADES                                          |
//+------------------------------------------------------------------+
void HandleGetHistoryTradesCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   long start_time = payload["start_time"].ToInteger();
   long end_time = payload["end_time"].ToInteger();

   if(!HistorySelect((datetime)start_time, (datetime)end_time))
   {
      SendErrorResponse(request_id, "Falha ao selecionar histórico de trades", response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";

   JSONNode trades_array;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(HistoryDealSelect(ticket))
      {
         JSONNode deal;
         deal["ticket"] = (long)ticket;
         deal["symbol"] = HistoryDealGetString(ticket, DEAL_SYMBOL);
         deal["type"] = HistoryDealGetInteger(ticket, DEAL_TYPE) == DEAL_TYPE_BUY ? "BUY" : "SELL";
         deal["volume"] = HistoryDealGetDouble(ticket, DEAL_VOLUME);
         deal["price"] = HistoryDealGetDouble(ticket, DEAL_PRICE);
         deal["profit"] = HistoryDealGetDouble(ticket, DEAL_PROFIT);
         deal["time"] = (long)HistoryDealGetInteger(ticket, DEAL_TIME);
         trades_array.Add(deal);
      }
   }
   response["trades"] = trades_array;
   SendJsonMessage(response, response_socket, socket_name);
}
//+------------------------------------------------------------------+
//| Bloco 3 - Comandos de Dados e Indicadores                       |
//| - Contém handlers para obter dados de mercado como OHLC, Tick,  |
//|   e indicadores (ex.: MA).                                      |
//| - Inclui funções auxiliares para processar indicadores e        |
//|   timeframes.                                                   |
//| - Este bloco será expandido para suportar novos indicadores     |
//|   (EMA, RSI) e timeframes personalizados (M2, M3, M10, H2).    |
//| - Futuro: Adicionar comando GET_SYMBOL_INFO para dados de ativos|
//|   (moeda, volume mínimo, swap, etc.).                          |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Função auxiliar para obter valores de indicadores               |
//+------------------------------------------------------------------+
bool CopyIndicatorBuffer(string symbol, ENUM_TIMEFRAMES timeframe, string indicator_type, int period, double &values[])
{
   if(indicator_type == "MA")
   {
      int handle = iMA(symbol, timeframe, period, 0, MODE_SMA, PRICE_CLOSE);
      if(handle == INVALID_HANDLE)
      {
         Print("ZmqTraderBridge ERROR: Falha ao criar handle do indicador MA");
         return false;
      }
      int copied = CopyBuffer(handle, 0, 0, 1, values);
      IndicatorRelease(handle);
      if(copied <= 0)
      {
         Print("ZmqTraderBridge ERROR: Falha ao obter valores do indicador MA");
         return false;
      }
      return true;
   }
   Print("ZmqTraderBridge ERROR: Indicador não suportado: ", indicator_type);
   return false;
}

//+------------------------------------------------------------------+
//| Comando GET_INDICATOR_MA                                        |
//+------------------------------------------------------------------+
void HandleGetIndicatorMACommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   string timeframe = payload["timeframe"].ToString();
   int period = (int)payload["period"].ToInteger();

   if(symbol == "" || period <= 0)
   {
      SendErrorResponse(request_id, "Parâmetros inválidos: symbol ou period", response_socket, socket_name);
      return;
   }

   ENUM_TIMEFRAMES tf = StringToTimeframe(timeframe);
   if(tf == PERIOD_CURRENT) tf = (ENUM_TIMEFRAMES)_Period;

   double ma_values[];
   ArrayResize(ma_values, 1);
   if(!CopyIndicatorBuffer(symbol, tf, "MA", period, ma_values))
   {
      SendErrorResponse(request_id, "Falha ao obter valores do indicador MA", response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["ma_value"] = ma_values[0];
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_OHLC                                                |
//+------------------------------------------------------------------+
void HandleGetOHLCCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   string timeframe = payload["timeframe"].ToString();

   if(symbol == "")
   {
      SendErrorResponse(request_id, "Parâmetro inválido: symbol", response_socket, socket_name);
      return;
   }

   ENUM_TIMEFRAMES tf = StringToTimeframe(timeframe);
   if(tf == PERIOD_CURRENT) tf = (ENUM_TIMEFRAMES)_Period;

   MqlRates rates[];
   ArrayResize(rates, 1);
   int copied = CopyRates(symbol, tf, 0, 1, rates);
   if(copied <= 0)
   {
      SendErrorResponse(request_id, "Falha ao obter dados OHLC", response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   JSONNode ohlc;
   ohlc["time"] = (long)rates[0].time;
   ohlc["open"] = rates[0].open;
   ohlc["high"] = rates[0].high;
   ohlc["low"] = rates[0].low;
   ohlc["close"] = rates[0].close;
   ohlc["volume"] = (long)rates[0].tick_volume;
   response["ohlc"] = ohlc;
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando GET_TICK                                                |
//+------------------------------------------------------------------+
void HandleGetTickCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();

   if(symbol == "")
   {
      SendErrorResponse(request_id, "Parâmetro inválido: symbol", response_socket, socket_name);
      return;
   }

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
   {
      SendErrorResponse(request_id, "Falha ao obter dados de tick", response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   JSONNode tick_data;
   tick_data["time"] = (long)tick.time;
   tick_data["bid"] = tick.bid;
   tick_data["ask"] = tick.ask;
   tick_data["volume"] = (long)tick.volume;
   response["tick"] = tick_data;
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Conversão de string para timeframe                              |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES StringToTimeframe(string tf)
{
   if(tf == "M1") return PERIOD_M1;
   if(tf == "M5") return PERIOD_M5;
   if(tf == "M15") return PERIOD_M15;
   if(tf == "M30") return PERIOD_M30;
   if(tf == "H1") return PERIOD_H1;
   if(tf == "H4") return PERIOD_H4;
   if(tf == "D1") return PERIOD_D1;
   if(tf == "W1") return PERIOD_W1;
   if(tf == "MN1") return PERIOD_MN1;
   return PERIOD_CURRENT;
}
//+------------------------------------------------------------------+
//| Bloco 4 - Comandos de Trading                                   |
//| - Contém handlers para operações de trading como compra, venda, |
//|   modificação e fechamento de posições e ordens.                |
//| - Este bloco processa comandos enviados pelo Python para        |
//|   executar operações no mercado via MetaTrader.                |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Comando TRADE_ORDER_TYPE_BUY                                    |
//+------------------------------------------------------------------+
void HandleTradeBuyCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   double volume = payload["volume"].ToDouble();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();
   int deviation = (int)payload["deviation"].ToInteger();
   string comment = payload["comment"].ToString();

   trade.SetDeviationInPoints(deviation);
   if(!trade.Buy(volume, symbol, price, sl, tp, comment))
   {
      SendErrorResponse(request_id, StringFormat("Falha na ordem BUY: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["deal"] = (long)trade.ResultDeal();
   response["order"] = (long)trade.ResultOrder();
   response["volume"] = trade.ResultVolume();
   response["price"] = trade.ResultPrice();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_ORDER_TYPE_SELL                                   |
//+------------------------------------------------------------------+
void HandleTradeSellCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   double volume = payload["volume"].ToDouble();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();
   int deviation = (int)payload["deviation"].ToInteger();
   string comment = payload["comment"].ToString();

   trade.SetDeviationInPoints(deviation);
   if(!trade.Sell(volume, symbol, price, sl, tp, comment))
   {
      SendErrorResponse(request_id, StringFormat("Falha na ordem SELL: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["deal"] = (long)trade.ResultDeal();
   response["order"] = (long)trade.ResultOrder();
   response["volume"] = trade.ResultVolume();
   response["price"] = trade.ResultPrice();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_ORDER_TYPE_BUY_LIMIT                              |
//+------------------------------------------------------------------+
void HandleTradeBuyLimitCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   double volume = payload["volume"].ToDouble();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();
   int deviation = (int)payload["deviation"].ToInteger();
   string comment = payload["comment"].ToString();

   trade.SetDeviationInPoints(deviation);
   if(!trade.BuyLimit(volume, price, symbol, sl, tp, 0, 0, comment))
   {
      SendErrorResponse(request_id, StringFormat("Falha na ordem BUY_LIMIT: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["deal"] = (long)trade.ResultDeal();
   response["order"] = (long)trade.ResultOrder();
   response["volume"] = trade.ResultVolume();
   response["price"] = trade.ResultPrice();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_ORDER_TYPE_SELL_LIMIT                             |
//+------------------------------------------------------------------+
void HandleTradeSellLimitCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   double volume = payload["volume"].ToDouble();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();
   int deviation = (int)payload["deviation"].ToInteger();
   string comment = payload["comment"].ToString();

   trade.SetDeviationInPoints(deviation);
   if(!trade.SellLimit(volume, price, symbol, sl, tp, 0, 0, comment))
   {
      SendErrorResponse(request_id, StringFormat("Falha na ordem SELL_LIMIT: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["deal"] = (long)trade.ResultDeal();
   response["order"] = (long)trade.ResultOrder();
   response["volume"] = trade.ResultVolume();
   response["price"] = trade.ResultPrice();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_ORDER_TYPE_BUY_STOP                               |
//+------------------------------------------------------------------+
void HandleTradeBuyStopCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   double volume = payload["volume"].ToDouble();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();
   int deviation = (int)payload["deviation"].ToInteger();
   string comment = payload["comment"].ToString();

   trade.SetDeviationInPoints(deviation);
   if(!trade.BuyStop(volume, price, symbol, sl, tp, 0, 0, comment))
   {
      SendErrorResponse(request_id, StringFormat("Falha na ordem BUY_STOP: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["deal"] = (long)trade.ResultDeal();
   response["order"] = (long)trade.ResultOrder();
   response["volume"] = trade.ResultVolume();
   response["price"] = trade.ResultPrice();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_ORDER_TYPE_SELL_STOP                              |
//+------------------------------------------------------------------+
void HandleTradeSellStopCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   double volume = payload["volume"].ToDouble();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();
   int deviation = (int)payload["deviation"].ToInteger();
   string comment = payload["comment"].ToString();

   trade.SetDeviationInPoints(deviation);
   if(!trade.SellStop(volume, price, symbol, sl, tp, 0, 0, comment))
   {
      SendErrorResponse(request_id, StringFormat("Falha na ordem SELL_STOP: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["deal"] = (long)trade.ResultDeal();
   response["order"] = (long)trade.ResultOrder();
   response["volume"] = trade.ResultVolume();
   response["price"] = trade.ResultPrice();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_POSITION_MODIFY                                   |
//+------------------------------------------------------------------+
void HandleTradePositionModifyCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   long ticket = payload["ticket"].ToInteger();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();

   if(!trade.PositionModify(ticket, sl, tp))
   {
      SendErrorResponse(request_id, StringFormat("Falha na modificação da posição: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_POSITION_PARTIAL                                  |
//+------------------------------------------------------------------+
void HandleTradePositionPartialCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   long ticket = payload["ticket"].ToInteger();
   double volume = payload["volume"].ToDouble();

   if(!PositionSelectByTicket(ticket))
   {
      SendErrorResponse(request_id, "Posição não encontrada", response_socket, socket_name);
      return;
   }
   if(!trade.PositionClosePartial(ticket, volume))
   {
      SendErrorResponse(request_id, StringFormat("Falha no fechamento parcial: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["deal"] = (long)trade.ResultDeal();
   response["order"] = (long)trade.ResultOrder();
   response["volume"] = trade.ResultVolume();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_POSITION_CLOSE_ID                                 |
//+------------------------------------------------------------------+
void HandleTradePositionCloseIdCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   long ticket = payload["ticket"].ToInteger();

   if(!trade.PositionClose(ticket))
   {
      SendErrorResponse(request_id, StringFormat("Falha no fechamento da posição: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["deal"] = (long)trade.ResultDeal();
   response["order"] = (long)trade.ResultOrder();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_POSITION_CLOSE(SYMBOL)                             |
//+------------------------------------------------------------------+
void HandleTradePositionCloseSymbolCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket) && PositionGetString(POSITION_SYMBOL) == symbol)
      {
         if(!trade.PositionClose(ticket))
         {
            SendErrorResponse(request_id, StringFormat("Falha no fechamento da posição: %s", trade.ResultComment()), response_socket, socket_name);
            return;
         }
      }
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = TRADE_RETCODE_DONE;
   response["result"] = "Positions closed";
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_ORDER_MODIFY                                      |
//+------------------------------------------------------------------+
void HandleTradeOrderModifyCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   long ticket = payload["ticket"].ToInteger();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();

   if(!trade.OrderModify(ticket, price, sl, tp, ORDER_TIME_GTC, 0))
   {
      SendErrorResponse(request_id, StringFormat("Falha na modificação da ordem: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando TRADE_ORDER_CANCEL                                      |
//+------------------------------------------------------------------+
void HandleTradeOrderCancelCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   long ticket = payload["ticket"].ToInteger();

   if(!trade.OrderDelete(ticket))
   {
      SendErrorResponse(request_id, StringFormat("Falha no cancelamento da ordem: %s", trade.ResultComment()), response_socket, socket_name);
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   SendJsonMessage(response, response_socket, socket_name);
}
//+------------------------------------------------------------------+
//| Bloco 5 - Streaming de Dados                                    |
//| - Contém handlers para iniciar/parar streaming de OHLC e       |
//|   indicadores, além da lógica de envio de atualizações.        |
//| - Este bloco será ajustado para consolidar respostas em uma    |
//|   única mensagem por ciclo do OnTimer (evitar respostas        |
//|   "picadas") e substituir streams existentes com mesmo       |
//|   request_id.                                                  |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Comando START_STREAM_OHLC                                       |
//+------------------------------------------------------------------+
void HandleStartStreamOHLCCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   string timeframe = payload["timeframe"].ToString();

   if(symbol == "" || timeframe == "")
   {
      SendErrorResponse(request_id, "Parâmetros inválidos: symbol ou timeframe", response_socket, socket_name);
      return;
   }

   ENUM_TIMEFRAMES tf = StringToTimeframe(timeframe);
   if(tf == PERIOD_CURRENT) tf = (ENUM_TIMEFRAMES)_Period;

   int index = ArraySize(g_stream_requests);
   ArrayResize(g_stream_requests, index + 1);
   g_stream_requests[index].symbol = symbol;
   g_stream_requests[index].timeframe = tf;
   g_stream_requests[index].request_id = request_id;
   g_stream_requests[index].last_sent_time = 0;

   g_streaming_active = true;
   if(InpDebugLog) PrintFormat("ZMQ Bridge: Iniciado streaming OHLC para %s, timeframe=%s", symbol, timeframe);

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["message"] = "Streaming OHLC iniciado";
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando STOP_STREAM                                             |
//+------------------------------------------------------------------+
void HandleStopStreamCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   string symbol = payload["symbol"].ToString();
   string timeframe = payload["timeframe"].ToString();

   bool found = false;
   for(int i = ArraySize(g_stream_requests) - 1; i >= 0; i--)
   {
      if(g_stream_requests[i].symbol == symbol && g_stream_requests[i].timeframe == StringToTimeframe(timeframe))
      {
         for(int j = i; j < ArraySize(g_stream_requests) - 1; j++)
         {
            g_stream_requests[j] = g_stream_requests[j + 1];
         }
         ArrayResize(g_stream_requests, ArraySize(g_stream_requests) - 1);
         found = true;
      }
   }

   if(!found)
   {
      SendErrorResponse(request_id, "Streaming não encontrado para symbol/timeframe", response_socket, socket_name);
      return;
   }

   g_streaming_active = ArraySize(g_stream_requests) > 0;
   if(InpDebugLog) PrintFormat("ZMQ Bridge: Streaming OHLC parado para %s, timeframe=%s", symbol, timeframe);

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["message"] = "Streaming OHLC parado";
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando START_STREAM_OHLC_INDICATORS                            |
//+------------------------------------------------------------------+
void HandleStartStreamOHLCIndicatorsCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   JSONNode *configs_node_ptr = payload["configs"];
   if(CheckPointer(configs_node_ptr) == POINTER_INVALID)
   {
      SendErrorResponse(request_id, "Payload sem configs array", response_socket, socket_name);
      return;
   }

   int configs_size = configs_node_ptr.Size();
   if(configs_size == 0)
   {
      SendErrorResponse(request_id, "Array de configs vazio", response_socket, socket_name);
      return;
   }

   // Limpar quaisquer streams existentes associados a este request_id
   for(int i = ArraySize(g_stream_requests) - 1; i >= 0; i--)
   {
      if(g_stream_requests[i].request_id == request_id)
      {
         for(int j = i; j < ArraySize(g_stream_requests) - 1; j++)
         {
            g_stream_requests[j] = g_stream_requests[j + 1];
         }
         ArrayResize(g_stream_requests, ArraySize(g_stream_requests) - 1);
      }
   }

   int config_count = 0;
   for(int i = 0; i < configs_size; i++)
   {
      JSONNode *config_node_ptr = (*configs_node_ptr)[i];
      if(CheckPointer(config_node_ptr) == POINTER_INVALID) continue;

      string symbol = config_node_ptr["symbol"].ToString();
      string timeframe = config_node_ptr["timeframe"].ToString();

      if(symbol == "" || timeframe == "")
      {
         SendErrorResponse(request_id, "Parâmetros inválidos: symbol ou timeframe na config " + IntegerToString(i), response_socket, socket_name);
         return;
      }

      ENUM_TIMEFRAMES tf = StringToTimeframe(timeframe);
      if(tf == PERIOD_CURRENT) tf = (ENUM_TIMEFRAMES)_Period;

      // Processar indicadores
      JSONNode *indicators_node_ptr = config_node_ptr["indicators"];
      IndicatorConfig indicators[];
      int indicator_count = 0;
      if(CheckPointer(indicators_node_ptr) != POINTER_INVALID)
      {
         int ind_size = indicators_node_ptr.Size();
         for(int j = 0; j < ind_size; j++)
         {
            JSONNode *ind_node_ptr = (*indicators_node_ptr)[j];
            if(CheckPointer(ind_node_ptr) == POINTER_INVALID) continue;

            string ind_type = ind_node_ptr["type"].ToString();
            int ind_period = (int)ind_node_ptr["period"].ToInteger();
            if(ind_type == "" || ind_period <= 0) continue;

            ArrayResize(indicators, indicator_count + 1);
            indicators[indicator_count].type = ind_type;
            indicators[indicator_count].period = ind_period;
            indicator_count++;
         }
      }

      // Adicionar ao array global g_stream_requests
      int index = ArraySize(g_stream_requests);
      ArrayResize(g_stream_requests, index + 1);
      g_stream_requests[index].symbol = symbol;
      g_stream_requests[index].timeframe = tf;
      g_stream_requests[index].request_id = request_id;
      g_stream_requests[index].last_sent_time = 0;

      ArrayResize(g_stream_requests[index].indicators, indicator_count);
      for(int j = 0; j < indicator_count; j++)
      {
         g_stream_requests[index].indicators[j].type = indicators[j].type;
         g_stream_requests[index].indicators[j].period = indicators[j].period;
      }

      config_count++;
   }

   g_streaming_active = true;
   if(InpDebugLog) PrintFormat("ZMQ Bridge: Iniciado streaming OHLC+Indicadores, configs=%d", config_count);

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["message"] = "Streaming OHLC+Indicadores iniciado";
   SendJsonMessage(response, response_socket, socket_name);
}

//+------------------------------------------------------------------+
//| Comando STOP_STREAM_OHLC_INDICATORS                             |
//+------------------------------------------------------------------+
void HandleStopStreamOHLCIndicatorsCommand(const string request_id, JSONNode &payload, Socket &response_socket, string socket_name)
{
   bool found = false;
   for(int i = ArraySize(g_stream_requests) - 1; i >= 0; i--)
   {
      if(g_stream_requests[i].request_id == request_id)
      {
         for(int j = i; j < ArraySize(g_stream_requests) - 1; j++)
         {
            g_stream_requests[j] = g_stream_requests[j + 1];
         }
         ArrayResize(g_stream_requests, ArraySize(g_stream_requests) - 1);
         found = true;
      }
   }

   if(!found)
   {
      SendErrorResponse(request_id, "Streaming OHLC+Indicadores não encontrado para request_id", response_socket, socket_name);
      return;
   }

   g_streaming_active = ArraySize(g_stream_requests) > 0;
   if(InpDebugLog) PrintFormat("ZMQ Bridge: Streaming OHLC+Indicadores parado para request_id=%s", request_id);

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["message"] = "Streaming OHLC+Indicadores parado";
   SendJsonMessage(response, response_socket, socket_name);
}
//+------------------------------------------------------------------+
//| Bloco 6 - Funções Principais do EA                              |
//| - Contém as funções principais do MetaTrader (OnInit, OnDeinit, |
//|   OnTimer, OnTradeTransaction) e lógica de controle para        |
//|   processar comandos recebidos via ZMQ.                        |
//| - Este bloco gerencia o ciclo de vida do EA e a execução de     |
//|   atualizações de streaming.                                   |
//| - Observação: Futuro suporte para priorização de respostas      |
//|   (ticks > dados de mercado > ordens > admin) pode ser          |
//|   implementado aqui no OnTimer.                                |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Inicialização do EA                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   Print("ZmqTraderBridge: Inicializando EA...");
   if(!ReadConfigFile(g_brokerKey, g_adminPort, g_dataPort, g_tradePort, g_livePort, g_strPort))
   {
      Alert("ZmqTraderBridge: Falha ao ler config.ini. Configuração obrigatória para instâncias.");
      return(INIT_PARAMETERS_INCORRECT);
   }
   if(StringLen(g_brokerKey) == 0 || StringFind(g_brokerKey, "-") <= 0)
   {
      Alert("ZmqTraderBridge: BrokerKey inválido!");
      return(INIT_PARAMETERS_INCORRECT);
   }

   if(!ValidatePorts())
      return(INIT_PARAMETERS_INCORRECT);

   admin_socket.setIdentity(g_brokerKey);
   if(!admin_socket.bind(StringFormat("tcp://*:%d", g_adminPort)))
   {
      PrintFormat("ZmqTraderBridge: Erro ao bind Admin Socket %d. GetLastError(): %d", g_adminPort, GetLastError());
      return(INIT_FAILED);
   }

   data_socket.setIdentity(g_brokerKey);
   if(!data_socket.bind(StringFormat("tcp://*:%d", g_dataPort)))
   {
      PrintFormat("ZmqTraderBridge: Erro ao bind Data Socket %d. GetLastError(): %d", g_dataPort, GetLastError());
      return(INIT_FAILED);
   }

   if(!trade_socket.bind(StringFormat("tcp://*:%d", g_tradePort)))
   {
      PrintFormat("ZmqTraderBridge: Erro ao bind Trade Socket %d. GetLastError(): %d", g_tradePort, GetLastError());
      return(INIT_FAILED);
   }
   trade_socket.subscribe("");

   if(!live_socket.bind(StringFormat("tcp://*:%d", g_livePort)))
   {
      PrintFormat("ZmqTraderBridge: Erro ao bind Live Socket %d. GetLastError(): %d", g_livePort, GetLastError());
      return(INIT_FAILED);
   }

   if(!stream_socket.bind(StringFormat("tcp://*:%d", g_strPort)))
   {
      PrintFormat("ZmqTraderBridge: Erro ao bind Streaming Socket %d. GetLastError(): %d", g_strPort, GetLastError());
      return(INIT_FAILED);
   }

   g_is_connected = true;
   if(!SendRegisterMessage(admin_socket, "Admin"))
      Print("ZmqTraderBridge: Falha ao enviar REGISTER.");
   if(!EventSetMillisecondTimer(InpTimerIntervalMs))
   {
      Print("ZmqTraderBridge: Erro ao iniciar Timer! GetLastError():", GetLastError());
      g_is_connected = false;
      return(INIT_FAILED);
   }
   Print("ZmqTraderBridge: Inicialização concluída.");
   g_last_ping_time = TimeCurrent();
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Desinicialização do EA                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   PrintFormat("ZmqTraderBridge: Desinicializando... Razão: %d", reason);
   if(g_is_connected)
      SendUnregisterMessage(admin_socket, "Admin");
   EventKillTimer();
   g_is_connected = false;

   admin_socket.disconnect(StringFormat("tcp://*:%d", g_adminPort));
   data_socket.disconnect(StringFormat("tcp://*:%d", g_dataPort));
   trade_socket.disconnect(StringFormat("tcp://*:%d", g_tradePort));
   live_socket.disconnect(StringFormat("tcp://*:%d", g_livePort));
   stream_socket.disconnect(StringFormat("tcp://*:%d", g_strPort));

   Print("ZmqTraderBridge: Desinicialização completa.");
}

//+------------------------------------------------------------------+
//| Timer do EA                                                     |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!g_is_connected) return;
   CheckIncomingCommands();

   if(g_streaming_active)
   {
      JSONNode grouped_msg;
      bool has_new_data = false;
      JSONNode data_array;

      for(int i = 0; i < ArraySize(g_stream_requests); i++)
      {
         string symbol = g_stream_requests[i].symbol;
         ENUM_TIMEFRAMES tf = g_stream_requests[i].timeframe;
         datetime last_sent = g_stream_requests[i].last_sent_time;

         MqlRates rates[];
         ArrayResize(rates, 1);
         int copied = CopyRates(symbol, tf, 0, 1, rates);
         if(copied > 0 && rates[0].time > last_sent)
         {
            JSONNode entry;
            entry["symbol"] = symbol;
            entry["timeframe"] = EnumToString(tf);

            JSONNode ohlc;
            ohlc["time"] = (long)rates[0].time;
            ohlc["open"] = rates[0].open;
            ohlc["high"] = rates[0].high;
            ohlc["low"] = rates[0].low;
            ohlc["close"] = rates[0].close;
            ohlc["volume"] = (long)rates[0].tick_volume;
            entry["ohlc"] = ohlc;

            JSONNode indicators_array;
            for(int j = 0; j < ArraySize(g_stream_requests[i].indicators); j++)
            {
               string ind_type = g_stream_requests[i].indicators[j].type;
               int period = g_stream_requests[i].indicators[j].period;
               double values[];
               ArrayResize(values, 1);
               if(CopyIndicatorBuffer(symbol, tf, ind_type, period, values))
               {
                  JSONNode ind;
                  ind["type"] = ind_type;
                  ind["period"] = period;
                  ind["value"] = values[0];
                  indicators_array.Add(ind);
               }
               else
               {
                  PrintFormat("ZMQ INDICATOR ERROR: Falha ao obter indicador %s(%d) para %s", ind_type, period, symbol);
               }
            }
            entry["indicators"] = indicators_array;

            data_array.Add(entry);
            g_stream_requests[i].last_sent_time = rates[0].time;
            has_new_data = true;
         }
      }

      if(has_new_data)
      {
         grouped_msg["type"] = "STREAM";
         grouped_msg["event"] = "OHLC_INDICATOR_UPDATE";
         grouped_msg["request_id"] = g_stream_requests[0].request_id;
         grouped_msg["timestamp_mql"] = (long)TimeCurrent();
         grouped_msg["data"] = data_array;

         if(SendJsonMessage(grouped_msg, live_socket, "Live"))
         {
            if(InpDebugLog)
               PrintFormat("ZMQ Bridge: Enviado OHLC+Indicadores para %d ativos, time=%s", ArraySize(g_stream_requests), TimeToString(TimeCurrent()));
         }
      }

      for(int i = 0; i < ArraySize(g_stream_requests); i++)
      {
         if(ArraySize(g_stream_requests[i].indicators) > 0) continue;

         string symbol = g_stream_requests[i].symbol;
         ENUM_TIMEFRAMES tf = g_stream_requests[i].timeframe;
         datetime last_sent = g_stream_requests[i].last_sent_time;

         MqlRates rates[];
         ArrayResize(rates, 1);
         int copied = CopyRates(symbol, tf, 0, 1, rates);
         if(copied > 0 && rates[0].time > last_sent)
         {
            JSONNode stream_msg;
            stream_msg["type"] = "STREAM";
            stream_msg["event"] = "OHLC_UPDATE";
            stream_msg["request_id"] = g_stream_requests[i].request_id;
            stream_msg["timestamp_mql"] = (long)TimeCurrent();

            JSONNode ohlc;
            ohlc["time"] = (long)rates[0].time;
            ohlc["open"] = rates[0].open;
            ohlc["high"] = rates[0].high;
            ohlc["low"] = rates[0].low;
            ohlc["close"] = rates[0].close;
            ohlc["volume"] = (long)rates[0].tick_volume;
            stream_msg["ohlc"] = ohlc;

            if(SendJsonMessage(stream_msg, live_socket, "Live"))
            {
               g_stream_requests[i].last_sent_time = rates[0].time;
               if(InpDebugLog)
                  PrintFormat("ZMQ Bridge: Enviado OHLC para %s, time=%s", symbol, TimeToString(rates[0].time));
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Processa comandos recebidos                                     |
//+------------------------------------------------------------------+
void CheckIncomingCommands()
{
   ZmqMsg msg_admin;
   while(admin_socket.recv(msg_admin, ZMQ_DONTWAIT))
   {
      string message_str = msg_admin.getData();
      if(InpDebugLog)
         PrintFormat("ZMQ RX (Admin): %s", message_str);
      JSONNode json_parser;
      if(json_parser.Deserialize(message_str))
      {
         ProcessCommand(json_parser, admin_socket, "Admin");
      }
      else
      {
         Print("ZMQ Bridge ERROR (Admin): Falha ao deserializar JSON: ", message_str);
      }
   }

   ZmqMsg msg_data;
   while(data_socket.recv(msg_data, ZMQ_DONTWAIT))
   {
      string message_str = msg_data.getData();
      if(InpDebugLog)
         PrintFormat("ZMQ RX (Data): %s", message_str);
      JSONNode json_parser;
      if(json_parser.Deserialize(message_str))
      {
         ProcessCommand(json_parser, data_socket, "Data");
      }
      else
      {
         Print("ZMQ Bridge ERROR (Data): Falha ao deserializar JSON: ", message_str);
      }
   }

   ZmqMsg msg_trade;
   while(trade_socket.recv(msg_trade, ZMQ_DONTWAIT))
   {
      string message_str = msg_trade.getData();
      if(InpDebugLog)
         PrintFormat("ZMQ RX (Trade): %s", message_str);
      JSONNode json_parser;
      if(json_parser.Deserialize(message_str))
      {
         ProcessCommand(json_parser, live_socket, "Live");
      }
      else
      {
         Print("ZMQ Bridge ERROR (Trade): Falha ao deserializar JSON: ", message_str);
      }
   }
}

//+------------------------------------------------------------------+
//| Processa comando JSON                                           |
//+------------------------------------------------------------------+
void ProcessCommand(JSONNode &json_command, Socket &response_socket, string socket_name)
{
   JSONNode *cmd_node_ptr = json_command["command"];
   JSONNode *reqid_node_ptr = json_command["request_id"];
   if(CheckPointer(cmd_node_ptr) == POINTER_INVALID || CheckPointer(reqid_node_ptr) == POINTER_INVALID)
   {
      SendErrorResponse("", "Comando sem 'command' ou 'request_id'", response_socket, socket_name);
      return;
   }

   string command = cmd_node_ptr.ToString();
   string request_id = reqid_node_ptr.ToString();
   JSONNode *payload_node_ptr = json_command["payload"];
   JSONNode payload = (CheckPointer(payload_node_ptr) != POINTER_INVALID) ? *payload_node_ptr : JSONNode();

   if(command == "PING")
   {
      HandlePingCommand(request_id, payload_node_ptr, response_socket, socket_name);
   }
   else if(command == "GET_STATUS_INFO")
   {
      HandleGetStatusInfoCommand(request_id, payload_node_ptr, response_socket, socket_name);
   }
   else if(command == "GET_BROKER_INFO")
   {
      HandleGetBrokerInfoCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_BROKER_SERVER")
   {
      HandleGetBrokerServerCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_BROKER_PATH")
   {
      HandleGetBrokerPathCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_ACCOUNT_INFO")
   {
      HandleGetAccountInfoCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_ACCOUNT_BALANCE")
   {
      HandleGetAccountBalanceCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_ACCOUNT_LEVERAGE")
   {
      HandleGetAccountLeverageCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_ACCOUNT_FLAGS")
   {
      HandleGetAccountFlagsCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_ACCOUNT_MARGIN")
   {
      HandleGetAccountMarginCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_ACCOUNT_STATE")
   {
      HandleGetAccountStateCommand(request_id, response_socket, socket_name);
   }
   else if(command == "GET_TIME_SERVER")
   {
      HandleGetTimeServerCommand(request_id, response_socket, socket_name);
   }
   else if(command == "POSITIONS")
   {
      HandleGetPositionsCommand(request_id, response_socket, socket_name);
   }
   else if(command == "ORDERS")
   {
      HandleGetOrdersCommand(request_id, response_socket, socket_name);
   }
   else if(command == "HISTORY_DATA")
   {
      HandleGetHistoryDataCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "HISTORY_TRADES")
   {
      HandleGetHistoryTradesCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_ORDER_TYPE_BUY")
   {
      HandleTradeBuyCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_ORDER_TYPE_SELL")
   {
      HandleTradeSellCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_ORDER_TYPE_BUY_LIMIT")
   {
      HandleTradeBuyLimitCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_ORDER_TYPE_SELL_LIMIT")
   {
      HandleTradeSellLimitCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_ORDER_TYPE_BUY_STOP")
   {
      HandleTradeBuyStopCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_ORDER_TYPE_SELL_STOP")
   {
      HandleTradeSellStopCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_POSITION_MODIFY")
   {
      HandleTradePositionModifyCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_POSITION_PARTIAL")
   {
      HandleTradePositionPartialCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_POSITION_CLOSE_ID")
   {
      HandleTradePositionCloseIdCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_POSITION_CLOSE")
   {
      HandleTradePositionCloseSymbolCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_ORDER_MODIFY")
   {
      HandleTradeOrderModifyCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "TRADE_ORDER_CANCEL")
   {
      HandleTradeOrderCancelCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "GET_INDICATOR_MA")
   {
      HandleGetIndicatorMACommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "GET_OHLC")
   {
      HandleGetOHLCCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "GET_TICK")
   {
      HandleGetTickCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "START_STREAM_OHLC")
   {
      HandleStartStreamOHLCCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "STOP_STREAM")
   {
      HandleStopStreamCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "START_STREAM_OHLC_INDICATORS")
   {
      HandleStartStreamOHLCIndicatorsCommand(request_id, payload, response_socket, socket_name);
   }
   else if(command == "STOP_STREAM_OHLC_INDICATORS")
   {
      HandleStopStreamOHLCIndicatorsCommand(request_id, payload, response_socket, socket_name);
   }
   else
   {
      SendErrorResponse(request_id, "Comando desconhecido: " + command, response_socket, socket_name);
   }
}

//+------------------------------------------------------------------+
//| Evento de transação de trading                                  |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   if(result.retcode == 0 || result.retcode == TRADE_RETCODE_NO_CHANGES)
   {
      return;
   }

   if(InpDebugLog)
   {
      PrintFormat("ZmqTraderBridge DEBUG: OnTradeTransaction - action=%s, retcode=%d, deal=%lld, order=%lld, symbol=%s, volume=%.2f",
                  EnumToString(request.action), result.retcode, result.deal, result.order, request.symbol, request.volume);
   }

   JSONNode stream_msg;
   stream_msg["type"] = "STREAM";
   stream_msg["event"] = "TRADE_EVENT";
   stream_msg["timestamp_mql"] = (long)TimeCurrent();

   JSONNode request_data;
   request_data["action"] = EnumToString(request.action);
   request_data["order"] = (long)request.order;
   request_data["symbol"] = request.symbol == "" ? NULL : request.symbol;
   request_data["volume"] = request.volume;
   request_data["price"] = request.price;
   request_data["sl"] = request.sl;
   request_data["tp"] = request.tp;
   request_data["deviation"] = (long)request.deviation;
   request_data["type"] = (int)request.type;
   request_data["type_filling"] = (int)request.type_filling;
   request_data["type_time"] = (int)request.type_time;
   request_data["comment"] = request.comment == "" ? NULL : request.comment;
   stream_msg["request"] = request_data;

   JSONNode result_data;
   result_data["retcode"] = (long)result.retcode;
   result_data["result"] = result.retcode == TRADE_RETCODE_DONE ? "TRADE_RETCODE_DONE" :
                           result.retcode == TRADE_RETCODE_ERROR ? "ERROR" :
                           IntegerToString(result.retcode);
   result_data["deal"] = (long)result.deal;
   result_data["order"] = (long)result.order;
   result_data["volume"] = result.volume;
   result_data["price"] = result.price;
   result_data["comment"] = result.comment == "" ? NULL : result.comment;
   stream_msg["result"] = result_data;

   if(result.retcode == TRADE_RETCODE_DONE || result.retcode == TRADE_RETCODE_REJECT ||
      result.retcode == TRADE_RETCODE_INVALID || result.retcode == TRADE_RETCODE_INVALID_PRICE)
   {
      if(!SendJsonMessage(stream_msg, stream_socket, "Streaming"))
      {
         Print("ZmqTraderBridge ERROR: Falha ao enviar mensagem de streaming");
      }
   }
   else if(InpDebugLog)
   {
      PrintFormat("ZmqTraderBridge DEBUG: Não enviando mensagem de streaming para retcode=%d", result.retcode);
   }
}