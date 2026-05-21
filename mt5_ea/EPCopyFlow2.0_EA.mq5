//+------------------------------------------------------------------+
//| EPCopyFlow 2.0                                                   |
//| EPCopyFlow2.0_EA.mq5                                             |
//| MQL5 <-> Python TCP Bridge para CopyTrade                        |
//| 1 socket TCP nativo (bidirecional), Python = servidor, EA = cliente.
//| Framing: 4 bytes big-endian length + UTF-8 JSON payload.         |
//| Modo MASTER: detecta trades e publica eventos                    |
//| Modo SLAVE: executa trades recebidos do Python                   |
//+------------------------------------------------------------------+
#property copyright "EPFilho"
#property link      "epfilho73@gmail.com"
#property version   "2.01"
#property strict

#include <Json.mqh>
#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| Bloco 1 - Configuração e Conexão TCP                             |
//+------------------------------------------------------------------+

//--- Parâmetros configuráveis pelo usuário (visíveis na janela de inputs do MT5)
input bool   InpDebugLog         = false;   // Ativar logs de debug

//--- Constantes do código (não configuráveis pelo MT5 — mudar aqui e recompilar)
const int    InpTimerIntervalMs  = 100;     // Intervalo do timer (ms)
const string InpTcpHost          = "127.0.0.1"; // Host do servidor Python (sempre localhost)
const int    InpConnectTimeoutMs = 1000;    // Timeout de conexão TCP (ms)

//--- Variáveis globais
int     g_socket = INVALID_HANDLE;  // Socket TCP nativo (EA = cliente)
bool    g_is_connected = false;
ulong   g_last_reconnect_attempt = 0;      // GetTickCount64() da última tentativa
const ulong RECONNECT_INTERVAL_MS = 2000;  // Tentar reconectar a cada 2s
uchar   g_rx_buffer[];               // Buffer de leitura (acumula bytes parciais)
int     g_rx_len = 0;                // Bytes válidos em g_rx_buffer
CTrade  trade;

//--- Config lidas do config.ini
string g_brokerKey = "";
string g_role = "SLAVE";  // MASTER ou SLAVE
int    g_commandPort = 0;
int    g_eventPort = 0;

//--- Monitoramento de trade_allowed
bool g_last_trade_allowed = false;
bool g_initial_trade_allowed_sent = false;

//--- Monitoramento de conexão com o servidor da corretora
bool g_last_terminal_connected = false;
bool g_initial_connection_status_sent = false;

//--- Push periódico de account_info (balance/equity/margin/profit/positions_count).
//--- Disparado a cada kAccountUpdateEvery ticks de OnTimer
//--- (kAccountUpdateEvery * InpTimerIntervalMs = intervalo efetivo).
const int kAccountUpdateEvery = 20;  // 20 * 100ms = 2s
int g_account_update_counter = 0;

//--- Magic number para identificar trades do CopyTrade.
//--- Fonte única: Python envia SET_MAGIC_NUMBER logo após receber REGISTER do EA.
//--- Até esse comando chegar, g_magic_number = 0 e a detecção de alien fica DESABILITADA
//--- (o sistema só detecta aliens após estar "pronto/conectado").
long g_magic_number = 0;

//--- Cache de posições para OnTrade() snapshot diff
#define MAX_CACHED_POSITIONS 64
struct CachedPosition {
   long   position_id;
   string symbol;
   double volume;
   double sl;
   double tp;
   long   pos_type;   // POSITION_TYPE_BUY=0, POSITION_TYPE_SELL=1
   long   magic;
};
CachedPosition g_pos_cache[MAX_CACHED_POSITIONS];
int g_pos_cache_size = 0;

//--- REGISTER retry (OnInit pode enviar antes do Python conectar)
bool g_register_sent = false;          // true quando REGISTER foi enviado com sucesso
int  g_register_retries = 0;           // Contador de tentativas

//--- OrderSendAsync: mapa de requests pendentes (request_id MQL5 → tcp_request_id)
//    Quando o broker responde, OnTradeTransaction recebe o resultado e envia a resposta TCP.
#define MAX_PENDING_REQUESTS 64
struct PendingTradeRequest {
   ulong  mql_request_id;     // request_id retornado por OrderSendAsync
   string tcp_request_id;     // request_id do Python (para enviar resposta via TCP)
   ulong  created_at;         // GetTickCount64() — para timeout/cleanup
   bool   is_used;            // slot ativo?
   // Contexto para verificação no timeout (#4): se a resposta async se perder
   // (result.request_id pode chegar 0 em execução assíncrona de bolsa), o EA
   // checa o estado real antes de reportar erro ao Python.
   string symbol;             // símbolo da ordem
   ulong  position_ticket;    // ticket alvo (close/partial); 0 numa abertura
   int    verify_kind;        // 0=abertura, 1=close total, 2=close parcial
};
PendingTradeRequest g_pending_requests[MAX_PENDING_REQUESTS];

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
bool ReadConfigFile(string &brokerKey, string &role, int &commandPort, int &eventPort)
{
   int file_handle = FileOpen("config.ini", FILE_READ|FILE_ANSI|FILE_TXT);
   if(file_handle == INVALID_HANDLE)
   {
      int error_code = GetLastError();
      Print("Erro ao abrir config.ini. Erro code = ", IntegerToString(error_code));
      string file_path = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\config.ini";
      Print("Caminho esperado: ", file_path);
      return false;
   }

   string currentSection = "";
   while(!FileIsEnding(file_handle))
   {
      string linha = FileReadString(file_handle);
      linha = TrimString(linha);

      // Detecta seções
      if(StringFind(linha, "[") == 0)
      {
         currentSection = linha;
         continue;
      }

      int posicaoIgual = StringFind(linha, "=");
      if(posicaoIgual <= 0) continue;

      string chave = TrimString(StringSubstr(linha, 0, posicaoIgual));
      string valor = TrimString(StringSubstr(linha, posicaoIgual + 1));

      if(currentSection == "[General]")
      {
         if(chave == "BrokerKey") brokerKey = valor;
         else if(chave == "Role") role = valor;
      }
      else if(currentSection == "[Ports]")
      {
         if(chave == "CommandPort") commandPort = (int)StringToInteger(valor);
         else if(chave == "EventPort") eventPort = (int)StringToInteger(valor);
      }
   }
   FileClose(file_handle);

   if(InpDebugLog)
   {
      PrintFormat("Config: BrokerKey=%s, Role=%s, CommandPort=%d, EventPort=%d",
                  brokerKey, role, commandPort, eventPort);
   }
   return true;
}

//+------------------------------------------------------------------+
//| Valida portas                                                    |
//+------------------------------------------------------------------+
bool ValidatePorts()
{
   if(g_commandPort == g_eventPort || g_commandPort <= 0 || g_eventPort <= 0)
   {
      Print("Erro: Portas inválidas ou duplicadas (CommandPort=", g_commandPort,
            ", EventPort=", g_eventPort, ")");
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| Serializa JSON de forma robusta                                 |
//+------------------------------------------------------------------+
void RobustJsonSerialize(JSONNode &json_message, string &out)
{
   // CRÍTICO: string NUNCA pode ser retornada via return — MQL5 trunca em ~255 chars.
   // Toda a cadeia usa passagem por referência (SerializeTo → out → SendJsonMessage).
   out = "";
   json_message.SerializeTo(out);

   if(StringLen(out) == 0)
   {
      // Fallback: Serialize() padrão (para Json.mqh sem SerializeTo)
      out = json_message.Serialize();
      if(StringLen(out) == 0)
      {
         Print("WARN: JSON serializado vazio");
         out = "{}";
      }
   }
}

//+------------------------------------------------------------------+
//| Bloco 1.1 - Camada TCP nativa (framing length-prefixed)          |
//+------------------------------------------------------------------+

// Forward declarations (usadas por TcpExtractAndProcessFrames)
void ProcessCommand(JSONNode &json_command);

//--- Serializa uint32 big-endian nos primeiros 4 bytes do buffer
void WriteBigEndianUint32(uchar &buffer[], int offset, uint value)
{
   buffer[offset + 0] = (uchar)((value >> 24) & 0xFF);
   buffer[offset + 1] = (uchar)((value >> 16) & 0xFF);
   buffer[offset + 2] = (uchar)((value >> 8)  & 0xFF);
   buffer[offset + 3] = (uchar)(value & 0xFF);
}

uint ReadBigEndianUint32(const uchar &buffer[], int offset)
{
   uint b0 = (uint)buffer[offset + 0];
   uint b1 = (uint)buffer[offset + 1];
   uint b2 = (uint)buffer[offset + 2];
   uint b3 = (uint)buffer[offset + 3];
   return (b0 << 24) | (b1 << 16) | (b2 << 8) | b3;
}

//--- Conecta ao servidor Python. Retorna true em sucesso.
bool TcpConnect()
{
   if(g_socket != INVALID_HANDLE)
   {
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
   }

   g_socket = SocketCreate();
   if(g_socket == INVALID_HANDLE)
   {
      PrintFormat("ERROR: SocketCreate falhou. GetLastError()=%d", GetLastError());
      return false;
   }

   if(!SocketConnect(g_socket, InpTcpHost, g_commandPort, InpConnectTimeoutMs))
   {
      if(InpDebugLog)
         PrintFormat("TCP: SocketConnect(%s:%d) falhou. GetLastError()=%d",
                     InpTcpHost, g_commandPort, GetLastError());
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      return false;
   }

   g_is_connected = true;
   g_rx_len = 0;
   ArrayResize(g_rx_buffer, 65536);
   PrintFormat("TCP conectado ao servidor Python em %s:%d", InpTcpHost, g_commandPort);
   return true;
}

//--- Fecha socket TCP
void TcpDisconnect()
{
   if(g_socket != INVALID_HANDLE)
   {
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
   }
   g_is_connected = false;
   g_rx_len = 0;
}

//--- Envia um frame [length BE][payload] pelo socket. Retorna true se todos os bytes foram enviados.
bool TcpSendFrame(const string payload)
{
   if(!g_is_connected || g_socket == INVALID_HANDLE)
      return false;

   uchar payload_bytes[];
   int payload_len = StringToCharArray(payload, payload_bytes, 0, -1, CP_UTF8);
   // StringToCharArray inclui null terminator se usado com -1; remover
   if(payload_len > 0 && payload_bytes[payload_len - 1] == 0)
      payload_len--;

   if(payload_len <= 0)
      return false;

   uchar frame[];
   ArrayResize(frame, 4 + payload_len);
   WriteBigEndianUint32(frame, 0, (uint)payload_len);
   for(int i = 0; i < payload_len; i++)
      frame[4 + i] = payload_bytes[i];

   int total = 4 + payload_len;
   int sent_total = 0;
   while(sent_total < total)
   {
      uchar chunk[];
      int remaining = total - sent_total;
      ArrayResize(chunk, remaining);
      for(int i = 0; i < remaining; i++)
         chunk[i] = frame[sent_total + i];

      int sent = SocketSend(g_socket, chunk, remaining);
      if(sent <= 0)
      {
         PrintFormat("ERROR: SocketSend falhou (sent=%d). GetLastError()=%d", sent, GetLastError());
         TcpDisconnect();
         return false;
      }
      sent_total += sent;
   }
   return true;
}

//--- Lê quantos bytes estiverem disponíveis para o buffer de RX acumulado.
void TcpPumpReads()
{
   if(!g_is_connected || g_socket == INVALID_HANDLE)
      return;

   uint available = SocketIsReadable(g_socket);
   if(available == 0)
      return;

   // Garante capacidade no buffer
   int needed = g_rx_len + (int)available;
   if(ArraySize(g_rx_buffer) < needed)
      ArrayResize(g_rx_buffer, needed + 4096);

   uchar tmp[];
   ArrayResize(tmp, (int)available);
   // Timeout 1ms: SocketIsReadable já confirmou que os bytes estão no buffer
   // do kernel. Bloco de 100ms (anterior) bloqueava a thread principal do MT5
   // e causava freeze visível ao tentar restaurar a janela do terminal.
   int read = SocketRead(g_socket, tmp, available, 1);
   if(read <= 0)
   {
      // Possivelmente desconectado
      if(!SocketIsConnected(g_socket))
      {
         Print("TCP: Conexão perdida durante leitura.");
         TcpDisconnect();
      }
      return;
   }
   for(int i = 0; i < read; i++)
      g_rx_buffer[g_rx_len + i] = tmp[i];
   g_rx_len += read;
}

//--- Extrai frames completos do buffer RX. Retorna JSONs para o callback.
void TcpExtractAndProcessFrames()
{
   while(g_rx_len >= 4)
   {
      uint payload_len = ReadBigEndianUint32(g_rx_buffer, 0);
      if(payload_len == 0 || payload_len > 16777216)  // 16 MiB cap
      {
         PrintFormat("ERROR: Frame length inválido (%u). Fechando conexão.", payload_len);
         TcpDisconnect();
         return;
      }

      int frame_size = 4 + (int)payload_len;
      if(g_rx_len < frame_size)
         return;  // frame incompleto, aguarda mais bytes

      // Extrai JSON
      uchar payload_bytes[];
      ArrayResize(payload_bytes, (int)payload_len + 1);
      for(int i = 0; i < (int)payload_len; i++)
         payload_bytes[i] = g_rx_buffer[4 + i];
      payload_bytes[payload_len] = 0;

      string message_str = CharArrayToString(payload_bytes, 0, (int)payload_len, CP_UTF8);

      // Remove frame do buffer: shift bytes restantes
      int remaining = g_rx_len - frame_size;
      for(int i = 0; i < remaining; i++)
         g_rx_buffer[i] = g_rx_buffer[frame_size + i];
      g_rx_len = remaining;

      // Processa o comando
      if(InpDebugLog)
         PrintFormat("RX: %s", message_str);

      JSONNode json_parser;
      if(json_parser.Deserialize(message_str))
      {
         ProcessCommand(json_parser);
      }
      else
      {
         Print("ERROR: Falha ao deserializar JSON: ", message_str);
      }
   }
}

//+------------------------------------------------------------------+
//| Enviar mensagem JSON pelo socket TCP (único, bidirecional)       |
//+------------------------------------------------------------------+
bool SendJsonMessage(JSONNode &json_message, string tag="TX")
{
   json_message["broker_key"] = g_brokerKey;
   if(!g_is_connected)
   {
      if(InpDebugLog)
         Print("WARN: Tentativa de envio sem conexão em ", tag);
      return false;
   }
   string message_str;
   RobustJsonSerialize(json_message, message_str);
   if(InpDebugLog)
      Print("TX (", tag, "): ", message_str);

   if(!TcpSendFrame(message_str))
   {
      PrintFormat("WARN: TcpSendFrame falhou em %s", tag);
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| Gerenciamento de requests pendentes (OrderSendAsync)             |
//+------------------------------------------------------------------+
void InitPendingRequests()
{
   for(int i = 0; i < MAX_PENDING_REQUESTS; i++)
      g_pending_requests[i].is_used = false;
}

bool AddPendingRequest(ulong mql_request_id, string tcp_request_id,
                       string symbol, ulong position_ticket, int verify_kind)
{
   for(int i = 0; i < MAX_PENDING_REQUESTS; i++)
   {
      if(!g_pending_requests[i].is_used)
      {
         g_pending_requests[i].mql_request_id  = mql_request_id;
         g_pending_requests[i].tcp_request_id  = tcp_request_id;
         g_pending_requests[i].created_at      = GetTickCount64();
         g_pending_requests[i].is_used         = true;
         g_pending_requests[i].symbol          = symbol;
         g_pending_requests[i].position_ticket = position_ticket;
         g_pending_requests[i].verify_kind     = verify_kind;
         return true;
      }
   }
   Print("WARN: Tabela de pending requests cheia (", MAX_PENDING_REQUESTS, ")");
   return false;
}

string FindAndRemovePendingRequest(ulong mql_request_id)
{
   for(int i = 0; i < MAX_PENDING_REQUESTS; i++)
   {
      if(g_pending_requests[i].is_used && g_pending_requests[i].mql_request_id == mql_request_id)
      {
         string tcp_id = g_pending_requests[i].tcp_request_id;
         g_pending_requests[i].is_used = false;
         return tcp_id;
      }
   }
   return "";
}

// #4: ao expirar um pending request a resposta async se perdeu. Antes de
// reportar erro ao Python, confirma o estado real — o trade pode ter
// executado mesmo sem a resposta ter casado. Retorna true se já respondeu OK.
bool VerifyPendingOutcome(string tcp_id, string symbol, ulong position_ticket, int verify_kind)
{
   if(verify_kind == 1)  // close total: a posição sumir == fechou
   {
      if(position_ticket > 0 && !PositionSelectByTicket(position_ticket))
      {
         JSONNode resp;
         resp["type"]       = "RESPONSE";
         resp["request_id"] = tcp_id;
         resp["status"]     = "OK";
         resp["retcode"]    = (long)TRADE_RETCODE_DONE;
         resp["result"]     = "Confirmado pos-timeout: posicao ja fechada";
         resp["ticket"]     = (long)position_ticket;
         SendJsonMessage(resp, "Command");
         PrintFormat("INFO: timeout resolvido — posicao %llu confirmada fechada (tcp_id=%s)",
                     position_ticket, tcp_id);
         return true;
      }
      return false;
   }

   if(verify_kind == 0)  // abertura: posição no símbolo com nosso magic == abriu
   {
      if(symbol != "" && PositionSelect(symbol)
         && PositionGetInteger(POSITION_MAGIC) == g_magic_number)
      {
         JSONNode resp;
         resp["type"]       = "RESPONSE";
         resp["request_id"] = tcp_id;
         resp["status"]     = "OK";
         resp["retcode"]    = (long)TRADE_RETCODE_DONE;
         resp["result"]     = "Confirmado pos-timeout: posicao aberta";
         resp["ticket"]     = (long)PositionGetInteger(POSITION_TICKET);
         resp["volume"]     = PositionGetDouble(POSITION_VOLUME);
         SendJsonMessage(resp, "Command");
         PrintFormat("INFO: timeout resolvido — posicao aberta em %s (tcp_id=%s)",
                     symbol, tcp_id);
         return true;
      }
      return false;
   }

   // verify_kind == 2 (close parcial): sem o estado pré-trade não dá pra
   // confirmar o volume — mantém o erro de timeout.
   return false;
}

void CleanupStalePendingRequests()
{
   // Remove requests com mais de 30 segundos (timeout de segurança)
   ulong now = GetTickCount64();
   for(int i = 0; i < MAX_PENDING_REQUESTS; i++)
   {
      if(g_pending_requests[i].is_used && (now - g_pending_requests[i].created_at) > 30000)
      {
         string tcp_id      = g_pending_requests[i].tcp_request_id;
         string symbol      = g_pending_requests[i].symbol;
         ulong  pos_ticket  = g_pending_requests[i].position_ticket;
         int    verify_kind = g_pending_requests[i].verify_kind;
         g_pending_requests[i].is_used = false;

         // #4: confirma o estado real antes de declarar falha.
         if(VerifyPendingOutcome(tcp_id, symbol, pos_ticket, verify_kind))
            continue;

         PrintFormat("WARN: Pending request expirado sem confirmacao: tcp_id=%s, symbol=%s",
                     tcp_id, symbol);
         SendErrorResponse(tcp_id, "Trade timeout (30s) no EA — estado nao confirmado");
      }
   }
}

//+------------------------------------------------------------------+
//| Determina filling mode suportado pelo símbolo                   |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING GetSymbolFillingMode(string symbol)
{
   long filling_mode = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((filling_mode & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   if((filling_mode & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

//+------------------------------------------------------------------+
//| Cotação atual de um símbolo (ASK p/ comprar, BID p/ vender).     |
//| Usa SymbolInfoTick em vez de SymbolInfoDouble: num símbolo       |
//| recém-adicionado ao Market Watch (ex.: virada de contrato no B3) |
//| SYMBOL_ASK/SYMBOL_BID podem vir 0 até chegar o primeiro tick.    |
//+------------------------------------------------------------------+
double GetMarketPrice(const string symbol, bool want_ask)
{
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return 0.0;
   double price = want_ask ? tick.ask : tick.bid;
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   return NormalizeDouble(price, digits);
}

//+------------------------------------------------------------------+
//| Mensagens de Sistema                                            |
//+------------------------------------------------------------------+
bool SendRegisterMessage()
{
   JSONNode message;
   message["type"] = "SYSTEM";
   message["event"] = "REGISTER";
   message["role"] = g_role;
   message["mt5_build"] = (long)TerminalInfoInteger(TERMINAL_BUILD);
   message["timestamp_mql"] = (long)TimeCurrent();
   PrintFormat("Enviando REGISTER para %s (Role=%s)", g_brokerKey, g_role);
   return SendJsonMessage(message, "Command");
}

bool SendUnregisterMessage()
{
   if(!g_is_connected) return false;
   JSONNode message;
   message["type"] = "SYSTEM";
   message["event"] = "UNREGISTER";
   message["timestamp_mql"] = (long)TimeCurrent();
   PrintFormat("Enviando UNREGISTER para %s", g_brokerKey);
   return SendJsonMessage(message, "Command");
}

//+------------------------------------------------------------------+
//| Resposta de erro padrão                                         |
//+------------------------------------------------------------------+
bool SendErrorResponse(const string request_id, const string error_message)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "ERROR";
   response["error_message"] = error_message;
   return SendJsonMessage(response, "Command");
}

//+------------------------------------------------------------------+
//| Push periódico: balance, equity, margin, free_margin, P/L atual  |
//| e P/L do dia.                                                    |
//|                                                                  |
//| Atribuição por role:                                             |
//|  - SLAVE: só conta posições/deals do CopyTrade (POSITION_MAGIC /  |
//|    DEAL_MAGIC == g_magic_number). Operações manuais/alien do      |
//|    titular não entram no P/L exibido pelo app.                    |
//|  - MASTER: conta tudo. O master só é usado para replicar, então   |
//|    toda posição/deal da conta é considerada do CopyTrade.          |
//|                                                                  |
//| P/L atual  = POSITION_PROFIT + POSITION_SWAP das posições abertas.|
//| P/L do dia = DEAL_PROFIT + DEAL_SWAP + DEAL_COMMISSION + DEAL_FEE |
//|   dos deals de saída (DEAL_ENTRY_OUT/INOUT/OUT_BY) desde a        |
//|   meia-noite do servidor.                                         |
//+------------------------------------------------------------------+
bool SendAccountUpdate()
{
   if(!g_is_connected) return false;

   // SLAVE com magic configurado filtra por magic; MASTER conta tudo.
   bool is_master      = (g_role == "MASTER");
   bool filter_by_magic = (!is_master && g_magic_number > 0);

   double total_profit = 0.0;
   int    positions_count = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket))
         continue;
      if(filter_by_magic && PositionGetInteger(POSITION_MAGIC) != g_magic_number)
         continue;
      total_profit += PositionGetDouble(POSITION_PROFIT);
      total_profit += PositionGetDouble(POSITION_SWAP);
      positions_count++;
   }

   // P/L do dia: resultado realizado dos deals de saída desde a meia-noite
   // do servidor.
   // TimeTradeServer() devolve a hora ATUAL calculada do servidor — avança
   // mesmo sem ticks novos. TimeCurrent() devolve a hora do ÚLTIMO tick, que
   // fica "presa" em ontem com o mercado fechado/pré-abertura, fazendo o P/L
   // do dia somar os deals de ONTEM. Fallback p/ TimeCurrent() se o offset do
   // servidor ainda não for conhecido (TimeTradeServer() == 0 logo no boot).
   datetime now_t = TimeTradeServer();
   if(now_t <= 0)
      now_t = TimeCurrent();
   datetime today_start = (datetime)((long)now_t - ((long)now_t % 86400));
   double daily_profit = 0.0;
   if(HistorySelect(today_start, now_t))
   {
      int total_deals = HistoryDealsTotal();
      for(int i = 0; i < total_deals; i++)
      {
         ulong deal_ticket = HistoryDealGetTicket(i);
         if(deal_ticket == 0) continue;

         // Só deals de trade (ignora BALANCE, CREDIT, CORRECTION, etc.)
         long deal_type = HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
         if(deal_type != DEAL_TYPE_BUY && deal_type != DEAL_TYPE_SELL)
            continue;

         if(filter_by_magic
            && HistoryDealGetInteger(deal_ticket, DEAL_MAGIC) != g_magic_number)
            continue;

         long entry = HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);
         if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT
            || entry == DEAL_ENTRY_OUT_BY)
         {
            daily_profit += HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
            daily_profit += HistoryDealGetDouble(deal_ticket, DEAL_SWAP);
            daily_profit += HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
            daily_profit += HistoryDealGetDouble(deal_ticket, DEAL_FEE);
         }
      }
   }

   JSONNode msg;
   msg["type"]            = "STREAM";
   msg["event"]           = "ACCOUNT_UPDATE";
   msg["timestamp_mql"]   = (long)now_t;
   msg["balance"]         = AccountInfoDouble(ACCOUNT_BALANCE);
   msg["equity"]          = AccountInfoDouble(ACCOUNT_EQUITY);
   msg["margin"]          = AccountInfoDouble(ACCOUNT_MARGIN);
   msg["free_margin"]     = AccountInfoDouble(ACCOUNT_FREEMARGIN);
   msg["currency"]        = AccountInfoString(ACCOUNT_CURRENCY);
   msg["profit"]          = total_profit;
   msg["daily_profit"]    = daily_profit;
   msg["positions_count"] = (long)positions_count;
   return SendJsonMessage(msg, "Event");
}

//+------------------------------------------------------------------+
//| Bloco 2 - Comandos Administrativos                              |
//| Respondidos por MASTER e SLAVE igualmente.                       |
//+------------------------------------------------------------------+

void HandlePingCommand(const string request_id, JSONNode *payload_node_ptr)
{
   if(InpDebugLog) Print("Recebido PING.");
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
   SendJsonMessage(response, "Command");
}

void HandleGetStatusInfoCommand(const string request_id, JSONNode *payload_node_ptr)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["trade_allowed"] = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
   response["balance"] = AccountInfoDouble(ACCOUNT_BALANCE);
   response["pong_timestamp_mql"] = (long)TimeCurrent();
   SendJsonMessage(response, "Command");
}

void HandleSetMagicNumberCommand(const string request_id, JSONNode &payload)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;

   JSONNode *magic_node = payload["magic_number"];
   if(CheckPointer(magic_node) == POINTER_INVALID)
   {
      response["status"] = "ERROR";
      response["error_message"] = "magic_number parameter required";
      SendJsonMessage(response, "Command");
      return;
   }

   long new_magic = magic_node.ToInteger();
   if(new_magic < 0)
   {
      response["status"] = "ERROR";
      response["error_message"] = "magic_number must be >= 0";
      SendJsonMessage(response, "Command");
      return;
   }

   g_magic_number = new_magic;
   trade.SetExpertMagicNumber((ulong)new_magic);

   PrintFormat("Magic number configurado via Python: %lld (alien detection %s)",
               g_magic_number, g_magic_number > 0 ? "ATIVO" : "DESABILITADO");

   // Catch-up do buffer de OnTrade: se trades ocorreram na janela
   // REGISTER → SET_MAGIC_NUMBER, OnTrade retornou cedo sem emitir nada e o
   // cache de posições continua no estado da inicialização. Dispara uma
   // varredura agora — o diff identifica qualquer abertura/fechamento/modify
   // que tenha ocorrido nesse intervalo e emite os eventos pendentes.
   if(g_role == "MASTER" && g_is_connected && new_magic > 0)
      OnTrade();

   response["status"] = "OK";
   response["magic_number"] = g_magic_number;
   SendJsonMessage(response, "Command");
}

void HandleGetAccountBalanceCommand(const string request_id)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["balance"] = AccountInfoDouble(ACCOUNT_BALANCE);
   response["equity"] = AccountInfoDouble(ACCOUNT_EQUITY);
   response["currency"] = AccountInfoString(ACCOUNT_CURRENCY);
   SendJsonMessage(response, "Command");
}

void HandleGetAccountFlagsCommand(const string request_id)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["trade_allowed"] = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
   response["expert_enabled"] = (bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT);
   SendJsonMessage(response, "Command");
}

void HandleGetAccountMarginCommand(const string request_id)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["margin"] = AccountInfoDouble(ACCOUNT_MARGIN);
   response["free_margin"] = AccountInfoDouble(ACCOUNT_FREEMARGIN);
   response["margin_level"] = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   SendJsonMessage(response, "Command");
}

void HandleGetSymbolInfoCommand(const string request_id, JSONNode &payload)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;

   // Extrair símbolo do payload
   JSONNode *symbol_node = payload["symbol"];
   if(CheckPointer(symbol_node) == POINTER_INVALID)
   {
      response["status"] = "ERROR";
      response["error_message"] = "symbol parameter required";
      SendJsonMessage(response, "Command");
      return;
   }

   string symbol = symbol_node.ToString();

   // Verificar se símbolo existe
   if(!SymbolSelect(symbol, true))
   {
      response["status"] = "ERROR";
      response["error_message"] = StringFormat("Symbol not found: %s", symbol);
      SendJsonMessage(response, "Command");
      return;
   }

   response["status"] = "OK";
   response["symbol"] = symbol;
   response["volume_min"] = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   response["volume_max"] = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   response["volume_step"] = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   response["digits"] = (long)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   response["trade_mode"] = (long)SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);

   SendJsonMessage(response, "Command");
}


void HandleGetPositionsCommand(const string request_id)
{
   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";

   // FLATTENAR para evitar bug de serialização de objetos aninhados em array
   int total = PositionsTotal();
   response["positions_count"] = (long)total;

   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
      {
         string prefix = StringFormat("pos_%d_", i);
         response[prefix + "ticket"] = (long)ticket;
         response[prefix + "symbol"] = PositionGetString(POSITION_SYMBOL);
         response[prefix + "type"] = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL";
         response[prefix + "volume"] = PositionGetDouble(POSITION_VOLUME);
         response[prefix + "price_open"] = PositionGetDouble(POSITION_PRICE_OPEN);
         response[prefix + "sl"] = PositionGetDouble(POSITION_SL);
         response[prefix + "tp"] = PositionGetDouble(POSITION_TP);
         response[prefix + "profit"] = PositionGetDouble(POSITION_PROFIT);
         response[prefix + "comment"] = PositionGetString(POSITION_COMMENT);
      }
   }
   SendJsonMessage(response, "Command");
}

void HandleGetOrdersCommand(const string request_id)
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
   SendJsonMessage(response, "Command");
}


void HandleGetHistoryTradesCommand(const string request_id, JSONNode &payload)
{
   long start_time = payload["start_time"].ToInteger();
   long end_time = payload["end_time"].ToInteger();

   if(start_time <= 0 || end_time <= 0 || start_time >= end_time)
   {
      end_time = TimeCurrent();
      start_time = end_time - 7 * 24 * 60 * 60;
   }

   if(!HistorySelect((datetime)start_time, (datetime)end_time))
   {
      SendErrorResponse(request_id, "Falha ao selecionar histórico");
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";

   struct PositionData {
      ulong position_id;
      string symbol;
      string type;
      double volume;
      double price_open;
      double price_close;
      double profit;
      long time_open;
      long time_close;
      string comment;
      bool has_entry_in;
      bool has_entry_out;
   };

   PositionData positions[];
   int position_count = 0;

   int total_deals = HistoryDealsTotal();
   CDealInfo dealInfo;

   for(int i = 0; i < total_deals; i++)
   {
      if(dealInfo.SelectByIndex(i))
      {
         ENUM_DEAL_TYPE deal_type = dealInfo.DealType();
         if(deal_type == DEAL_TYPE_BUY || deal_type == DEAL_TYPE_SELL)
         {
            ulong position_id = dealInfo.PositionId();
            ENUM_DEAL_ENTRY entry = dealInfo.Entry();

            int pos_index = -1;
            for(int j = 0; j < position_count; j++)
            {
               if(positions[j].position_id == position_id)
               {
                  pos_index = j;
                  break;
               }
            }

            if(pos_index == -1)
            {
               ArrayResize(positions, position_count + 1);
               pos_index = position_count;
               positions[pos_index].position_id = position_id;
               positions[pos_index].symbol = dealInfo.Symbol();
               positions[pos_index].type = deal_type == DEAL_TYPE_BUY ? "BUY" : "SELL";
               positions[pos_index].volume = dealInfo.Volume();
               positions[pos_index].comment = dealInfo.Comment();
               positions[pos_index].has_entry_in = false;
               positions[pos_index].has_entry_out = false;
               positions[pos_index].profit = 0;
               position_count++;
            }

            if(entry == DEAL_ENTRY_IN)
            {
               positions[pos_index].price_open = dealInfo.Price();
               positions[pos_index].time_open = (long)dealInfo.Time();
               positions[pos_index].has_entry_in = true;
            }
            else if(entry == DEAL_ENTRY_OUT)
            {
               positions[pos_index].price_close = dealInfo.Price();
               positions[pos_index].time_close = (long)dealInfo.Time();
               positions[pos_index].profit += dealInfo.Profit();
               positions[pos_index].has_entry_out = true;
            }
         }
      }
   }

   JSONNode positions_array;
   for(int i = 0; i < position_count; i++)
   {
      if(positions[i].has_entry_in && positions[i].has_entry_out)
      {
         JSONNode position;
         position["ticket"] = (long)positions[i].position_id;
         position["symbol"] = positions[i].symbol;
         position["type"] = positions[i].type;
         position["volume"] = positions[i].volume;
         position["price_open"] = positions[i].price_open;
         position["price_close"] = positions[i].price_close;
         position["profit"] = positions[i].profit;
         position["time_open"] = positions[i].time_open;
         position["time_close"] = positions[i].time_close;
         position["comment"] = positions[i].comment;
         positions_array.Add(position);
      }
   }

   response["positions"] = positions_array;
   SendJsonMessage(response, "Command");
}

//+------------------------------------------------------------------+
//| Bloco 3 - Comandos de Trading                                   |
//| Executados apenas pelo SLAVE. MASTER rejeita comandos de trade.  |
//+------------------------------------------------------------------+

void HandleTradeBuyCommand(const string request_id, JSONNode &payload)
{
   if(g_role == "MASTER")
   {
      SendErrorResponse(request_id, "MASTER não aceita comandos de trade");
      return;
   }

   string symbol = payload["symbol"].ToString();

   // Garante que o símbolo existe no broker e está no Market Watch.
   // Crítico em B3 ao virar contrato (WINQ25 → WINV25): o slave pode nunca
   // ter operado o símbolo novo. SymbolSelect também adiciona ao Market Watch.
   if(!SymbolSelect(symbol, true))
   {
      SendErrorResponse(request_id, StringFormat("Símbolo %s não disponível no broker", symbol));
      return;
   }

   double volume = payload["volume"].ToDouble();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();
   int deviation = (int)payload["deviation"].ToInteger();
   string comment = payload["comment"].ToString();

   if(price <= 0)
      price = GetMarketPrice(symbol, true);

   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action = TRADE_ACTION_DEAL;
   req.symbol = symbol;
   req.volume = volume;
   req.type = ORDER_TYPE_BUY;
   req.price = price;
   req.sl = sl;
   req.tp = tp;
   req.deviation = (ulong)deviation;
   req.magic = (ulong)g_magic_number;
   req.comment = comment;
   req.type_filling = GetSymbolFillingMode(symbol);

   if(!OrderSendAsync(req, res))
   {
      SendErrorResponse(request_id, StringFormat("OrderSendAsync BUY falhou: retcode=%d, %s",
                        res.retcode, res.comment));
      return;
   }

   if(!AddPendingRequest((ulong)res.request_id, request_id, symbol, 0, 0))
   {
      SendErrorResponse(request_id, "Pending requests table full");
      return;
   }

   if(InpDebugLog)
      PrintFormat("OrderSendAsync BUY: symbol=%s, vol=%.2f, mql_req=%u, tcp_req=%s",
                  symbol, volume, res.request_id, request_id);
}

void HandleTradeSellCommand(const string request_id, JSONNode &payload)
{
   if(g_role == "MASTER")
   {
      SendErrorResponse(request_id, "MASTER não aceita comandos de trade");
      return;
   }

   string symbol = payload["symbol"].ToString();

   if(!SymbolSelect(symbol, true))
   {
      SendErrorResponse(request_id, StringFormat("Símbolo %s não disponível no broker", symbol));
      return;
   }

   double volume = payload["volume"].ToDouble();
   double price = payload["price"].ToDouble();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();
   int deviation = (int)payload["deviation"].ToInteger();
   string comment = payload["comment"].ToString();

   if(price <= 0)
      price = GetMarketPrice(symbol, false);

   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action = TRADE_ACTION_DEAL;
   req.symbol = symbol;
   req.volume = volume;
   req.type = ORDER_TYPE_SELL;
   req.price = price;
   req.sl = sl;
   req.tp = tp;
   req.deviation = (ulong)deviation;
   req.magic = (ulong)g_magic_number;
   req.comment = comment;
   req.type_filling = GetSymbolFillingMode(symbol);

   if(!OrderSendAsync(req, res))
   {
      SendErrorResponse(request_id, StringFormat("OrderSendAsync SELL falhou: retcode=%d, %s",
                        res.retcode, res.comment));
      return;
   }

   if(!AddPendingRequest((ulong)res.request_id, request_id, symbol, 0, 0))
   {
      SendErrorResponse(request_id, "Pending requests table full");
      return;
   }

   if(InpDebugLog)
      PrintFormat("OrderSendAsync SELL: symbol=%s, vol=%.2f, mql_req=%u, tcp_req=%s",
                  symbol, volume, res.request_id, request_id);
}

void HandleTradePositionModifyCommand(const string request_id, JSONNode &payload)
{
   if(g_role == "MASTER")
   {
      SendErrorResponse(request_id, "MASTER não aceita comandos de trade");
      return;
   }

   long ticket = payload["ticket"].ToInteger();
   double sl = payload["sl"].ToDouble();
   double tp = payload["tp"].ToDouble();

   if(!trade.PositionModify(ticket, sl, tp))
   {
      SendErrorResponse(request_id, StringFormat("Falha modificar posição: %s", trade.ResultComment()));
      return;
   }

   JSONNode response;
   response["type"] = "RESPONSE";
   response["request_id"] = request_id;
   response["status"] = "OK";
   response["retcode"] = (long)trade.ResultRetcode();
   response["result"] = trade.ResultComment();
   response["ticket"] = ticket;
   SendJsonMessage(response, "Command");
}

void HandleTradePositionCloseIdCommand(const string request_id, JSONNode &payload)
{
   // Emergency close bypassa proteção do MASTER
   bool is_emergency = false;
   JSONNode *emergency_node = payload["emergency"];
   if(CheckPointer(emergency_node) != POINTER_INVALID)
      is_emergency = (emergency_node.ToBool() || emergency_node.ToString() == "true");

   if(g_role == "MASTER" && !is_emergency)
   {
      SendErrorResponse(request_id, "MASTER não aceita comandos de trade");
      return;
   }

   long ticket = payload["ticket"].ToInteger();

   if(!PositionSelectByTicket(ticket))
   {
      SendErrorResponse(request_id, "Posição não encontrada");
      return;
   }

   string symbol = PositionGetString(POSITION_SYMBOL);
   double volume = PositionGetDouble(POSITION_VOLUME);
   ENUM_POSITION_TYPE pos_type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action = TRADE_ACTION_DEAL;
   req.symbol = symbol;
   req.volume = volume;
   req.position = (ulong)ticket;
   req.type = (pos_type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   req.price = GetMarketPrice(symbol, pos_type != POSITION_TYPE_BUY);
   req.deviation = 100;
   req.magic = (ulong)g_magic_number;
   req.type_filling = GetSymbolFillingMode(symbol);

   if(!OrderSendAsync(req, res))
   {
      SendErrorResponse(request_id, StringFormat("OrderSendAsync CLOSE falhou: retcode=%d, %s",
                        res.retcode, res.comment));
      return;
   }

   if(!AddPendingRequest((ulong)res.request_id, request_id, symbol, (ulong)ticket, 1))
   {
      SendErrorResponse(request_id, "Pending requests table full");
      return;
   }

   if(InpDebugLog)
      PrintFormat("OrderSendAsync CLOSE: ticket=%lld, symbol=%s, mql_req=%u, tcp_req=%s",
                  ticket, symbol, res.request_id, request_id);
}

//+------------------------------------------------------------------+
//| Bloco 4 - Funções Principais do EA                              |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Inicialização do EA                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   Print("EPCopyFlow EA: Inicializando...");

   if(!ReadConfigFile(g_brokerKey, g_role, g_commandPort, g_eventPort))
   {
      Alert("EPCopyFlow EA: Falha ao ler config.ini.");
      return(INIT_PARAMETERS_INCORRECT);
   }

   if(StringLen(g_brokerKey) == 0 || StringFind(g_brokerKey, "-") <= 0)
   {
      Alert("EPCopyFlow EA: BrokerKey inválido!");
      return(INIT_PARAMETERS_INCORRECT);
   }

   // Validar Role
   if(g_role != "MASTER" && g_role != "SLAVE")
   {
      Alert("EPCopyFlow EA: Role inválido! Deve ser MASTER ou SLAVE. Recebido: ", g_role);
      return(INIT_PARAMETERS_INCORRECT);
   }

   if(!ValidatePorts())
      return(INIT_PARAMETERS_INCORRECT);

   // Conectar via TCP nativo (Python é o servidor).
   // Se a conexão falhar aqui, o OnTimer fará retry periódico.
   InitPendingRequests();
   if(TcpConnect())
   {
      if(SendRegisterMessage())
      {
         g_register_sent = true;
         g_register_retries = 0;
      }
      else
      {
         Print("REGISTER falhou no OnInit. Retry via OnTimer.");
         g_register_sent = false;
         g_register_retries = 0;
      }
   }
   else
   {
      Print("TCP connect falhou no OnInit (Python pode não estar escutando ainda). Retry via OnTimer.");
      g_register_sent = false;
      g_register_retries = 0;
   }
   g_last_reconnect_attempt = GetTickCount64();

   if(!EventSetMillisecondTimer(InpTimerIntervalMs))
   {
      Print("Erro ao iniciar Timer! GetLastError():", GetLastError());
      g_is_connected = false;
      return(INIT_FAILED);
   }

   g_last_trade_allowed = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
   g_last_terminal_connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);

   RefreshPositionCache();

   PrintFormat("EPCopyFlow EA: Inicializado. Role=%s, BrokerKey=%s, cached_positions=%d, TimerInterval=%dms",
               g_role, g_brokerKey, g_pos_cache_size, InpTimerIntervalMs);
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Desinicialização do EA                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   PrintFormat("EPCopyFlow EA: Desinicializando... Razão: %d", reason);
   if(g_is_connected)
      SendUnregisterMessage();
   EventKillTimer();
   TcpDisconnect();
   Print("EPCopyFlow EA: Desinicialização completa.");
}

//+------------------------------------------------------------------+
//| OnTimer                                                          |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Reconexão: se não há socket, tenta conectar periodicamente ao Python.
   if(!g_is_connected)
   {
      ulong now = GetTickCount64();
      if(now - g_last_reconnect_attempt >= RECONNECT_INTERVAL_MS)
      {
         g_last_reconnect_attempt = now;
         if(TcpConnect())
         {
            // Força reenvio de REGISTER e estados iniciais na nova sessão
            g_register_sent = false;
            g_register_retries = 0;
            g_initial_trade_allowed_sent = false;
            g_initial_connection_status_sent = false;
         }
      }
      return;
   }

   // Sanidade: verifica se o socket ainda está conectado
   if(g_socket == INVALID_HANDLE || !SocketIsConnected(g_socket))
   {
      Print("TCP: SocketIsConnected() retornou false. Desconectando para reconexão.");
      TcpDisconnect();
      return;
   }

   // Retry REGISTER se falhou (Python pode não ter aceitado ainda)
   if(!g_register_sent && g_register_retries < 30)
   {
      g_register_retries++;
      if(SendRegisterMessage())
      {
         g_register_sent = true;
         PrintFormat("REGISTER enviado com sucesso na tentativa %d.", g_register_retries);
      }
   }

   // Processa comandos recebidos via TCP
   CheckIncomingCommands();

   // Envio inicial de trade_allowed
   if(!g_initial_trade_allowed_sent)
   {
      bool current = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
      JSONNode msg;
      msg["type"] = "STREAM";
      msg["event"] = "TRADE_ALLOWED_UPDATE";
      msg["trade_allowed"] = current;
      msg["timestamp_mql"] = (long)TimeCurrent();
      SendJsonMessage(msg, "Event");
      g_initial_trade_allowed_sent = true;
      g_last_trade_allowed = current;
   }

   // Detecta mudança de trade_allowed
   bool current_trade_allowed = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
   if(current_trade_allowed != g_last_trade_allowed)
   {
      JSONNode msg;
      msg["type"] = "STREAM";
      msg["event"] = "TRADE_ALLOWED_UPDATE";
      msg["trade_allowed"] = current_trade_allowed;
      msg["timestamp_mql"] = (long)TimeCurrent();
      SendJsonMessage(msg, "Event");
      g_last_trade_allowed = current_trade_allowed;
      if(InpDebugLog)
         PrintFormat("TRADE_ALLOWED_UPDATE: %s", current_trade_allowed ? "true" : "false");
   }

   // Envio inicial de connection_status
   if(!g_initial_connection_status_sent)
   {
      bool connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
      JSONNode msg;
      msg["type"] = "STREAM";
      msg["event"] = "CONNECTION_STATUS";
      msg["connected"] = connected;
      msg["timestamp_mql"] = (long)TimeCurrent();
      SendJsonMessage(msg, "Event");
      g_initial_connection_status_sent = true;
      g_last_terminal_connected = connected;
   }

   // Detecta mudança de conexão com o servidor da corretora
   bool current_connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   if(current_connected != g_last_terminal_connected)
   {
      JSONNode msg;
      msg["type"] = "STREAM";
      msg["event"] = "CONNECTION_STATUS";
      msg["connected"] = current_connected;
      msg["timestamp_mql"] = (long)TimeCurrent();
      SendJsonMessage(msg, "Event");
      g_last_terminal_connected = current_connected;
      if(InpDebugLog)
         PrintFormat("CONNECTION_STATUS: %s", current_connected ? "connected" : "disconnected");
   }

   // Push periódico de account_info (balance/equity/margin/profit/positions_count)
   g_account_update_counter++;
   if(g_account_update_counter >= kAccountUpdateEvery)
   {
      g_account_update_counter = 0;
      SendAccountUpdate();
   }

   // Limpar requests assíncronos expirados (timeout 30s)
   CleanupStalePendingRequests();
}

//+------------------------------------------------------------------+
//| Processa comandos recebidos via TCP                              |
//+------------------------------------------------------------------+
void CheckIncomingCommands()
{
   // Drena todos os bytes disponíveis no socket e extrai frames completos
   TcpPumpReads();
   TcpExtractAndProcessFrames();
}

//+------------------------------------------------------------------+
//| Processa comando JSON                                           |
//+------------------------------------------------------------------+
void ProcessCommand(JSONNode &json_command)
{
   JSONNode *cmd_node_ptr = json_command["command"];
   JSONNode *reqid_node_ptr = json_command["request_id"];
   if(CheckPointer(cmd_node_ptr) == POINTER_INVALID || CheckPointer(reqid_node_ptr) == POINTER_INVALID)
   {
      SendErrorResponse("", "Comando sem 'command' ou 'request_id'");
      return;
   }

   string command = cmd_node_ptr.ToString();
   string request_id = reqid_node_ptr.ToString();
   JSONNode *payload_node_ptr = json_command["payload"];
   JSONNode payload = (CheckPointer(payload_node_ptr) != POINTER_INVALID) ? *payload_node_ptr : JSONNode();

   // ── Comandos Admin (MASTER + SLAVE) ──
   if(command == "SET_MAGIC_NUMBER")
   {
      HandleSetMagicNumberCommand(request_id, payload);
   }
   else if(command == "PING")
   {
      HandlePingCommand(request_id, payload_node_ptr);
   }
   else if(command == "GET_STATUS_INFO")
   {
      HandleGetStatusInfoCommand(request_id, payload_node_ptr);
   }
   else if(command == "GET_ACCOUNT_BALANCE")
   {
      HandleGetAccountBalanceCommand(request_id);
   }
   else if(command == "GET_ACCOUNT_FLAGS")
   {
      HandleGetAccountFlagsCommand(request_id);
   }
   else if(command == "GET_ACCOUNT_MARGIN")
   {
      HandleGetAccountMarginCommand(request_id);
   }
   else if(command == "GET_SYMBOL_INFO")
   {
      HandleGetSymbolInfoCommand(request_id, payload);
   }
   else if(command == "POSITIONS" || command == "GET_POSITIONS")
   {
      HandleGetPositionsCommand(request_id);
   }
   else if(command == "ORDERS")
   {
      HandleGetOrdersCommand(request_id);
   }
   else if(command == "HISTORY_TRADES")
   {
      HandleGetHistoryTradesCommand(request_id, payload);
   }
   // ── Comandos de Trade (SLAVE only - MASTER rejeita dentro dos handlers) ──
   else if(command == "TRADE_ORDER_TYPE_BUY")
   {
      HandleTradeBuyCommand(request_id, payload);
   }
   else if(command == "TRADE_ORDER_TYPE_SELL")
   {
      HandleTradeSellCommand(request_id, payload);
   }
   else if(command == "TRADE_POSITION_MODIFY")
   {
      HandleTradePositionModifyCommand(request_id, payload);
   }
   else if(command == "TRADE_POSITION_CLOSE_ID")
   {
      HandleTradePositionCloseIdCommand(request_id, payload);
   }
   else
   {
      SendErrorResponse(request_id, "Comando desconhecido: " + command);
   }
}

//+------------------------------------------------------------------+
//| Bloco 4b - OnTrade() snapshot diff (MASTER only)                 |
//| Detecta fechamentos por SL/TP/SO e modificações de SL/TP.        |
//| Compara cache de posições vs estado atual e emite eventos.        |
//+------------------------------------------------------------------+
int BuildPositionSnapshot(CachedPosition &snap[])
{
   int count = 0;
   for(int i = 0; i < PositionsTotal() && count < MAX_CACHED_POSITIONS; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket))
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);

      // MASTER: rastrear TODAS as posições (usuário opera manualmente, magic=0)
      // SLAVE: só nossas posições (magic match) — mas OnTrade() já retorna cedo para SLAVE
      if(g_role == "SLAVE" && g_magic_number > 0 && magic != g_magic_number)
         continue;

      snap[count].position_id = PositionGetInteger(POSITION_IDENTIFIER);
      snap[count].symbol      = PositionGetString(POSITION_SYMBOL);
      snap[count].volume      = PositionGetDouble(POSITION_VOLUME);
      snap[count].sl          = PositionGetDouble(POSITION_SL);
      snap[count].tp          = PositionGetDouble(POSITION_TP);
      snap[count].pos_type    = PositionGetInteger(POSITION_TYPE);
      snap[count].magic       = magic;
      count++;
   }

   if(PositionsTotal() > MAX_CACHED_POSITIONS)
      PrintFormat("WARN: %d posições abertas excedem o cache (%d) — o diff de OnTrade pode perder eventos das posições excedentes",
                  PositionsTotal(), MAX_CACHED_POSITIONS);

   return count;
}

void RefreshPositionCache()
{
   g_pos_cache_size = BuildPositionSnapshot(g_pos_cache);
}

void EmitSyntheticTradeEvent(const CachedPosition &cached, double closed_volume, double remaining_volume)
{
   JSONNode stream_msg;
   stream_msg["type"]       = "STREAM";
   stream_msg["event"]      = "TRADE_EVENT";
   stream_msg["timestamp_mql"] = (long)TimeCurrent();
   stream_msg["role"]       = g_role;
   stream_msg["source"]     = "ONTRADE";

   stream_msg["request_action"]       = 1;  // TRADE_ACTION_DEAL
   stream_msg["request_order"]        = (long)0;
   stream_msg["request_symbol"]       = cached.symbol;
   stream_msg["request_volume"]       = closed_volume;
   stream_msg["request_price"]        = 0.0;
   stream_msg["request_sl"]           = 0.0;
   stream_msg["request_tp"]           = 0.0;
   stream_msg["request_deviation"]    = (long)0;
   stream_msg["request_type"]         = (cached.pos_type == POSITION_TYPE_BUY) ? 1 : 0;
   stream_msg["request_type_filling"] = 0;
   stream_msg["request_comment"]      = "";
   stream_msg["request_position"]     = cached.position_id;

   stream_msg["result_retcode"] = (long)TRADE_RETCODE_DONE;
   stream_msg["result_deal"]    = (long)0;
   stream_msg["result_order"]   = (long)0;
   stream_msg["result_volume"]  = closed_volume;
   stream_msg["result_price"]   = 0.0;
   stream_msg["result_comment"] = "detected by OnTrade";

   stream_msg["position_volume_remaining"] = remaining_volume;
   stream_msg["position_id"]               = cached.position_id;

   if(!SendJsonMessage(stream_msg, "Event"))
      Print("ERROR: Falha ao enviar TRADE_EVENT sintético via EventSocket");

   PrintFormat("OnTrade: %s detectado (pos_id=%lld, symbol=%s, vol=%.2f, remaining=%.2f)",
               (remaining_volume > 0) ? "PARTIAL_CLOSE" : "CLOSE",
               cached.position_id, cached.symbol, closed_volume, remaining_volume);
}

void EmitReversalEvent(const CachedPosition &old_pos, const CachedPosition &new_pos)
{
   // Em netting, uma ordem oposta de volume maior que a posição atual faz o
   // POSITION_IDENTIFIER permanecer, POSITION_TYPE inverter e POSITION_VOLUME
   // virar o excedente na direção nova. OnTradeTransaction reporta a ordem
   // original (volume total), mas não sinaliza a inversão — detectamos aqui.
   JSONNode stream_msg;
   stream_msg["type"]       = "STREAM";
   stream_msg["event"]      = "TRADE_EVENT";
   stream_msg["timestamp_mql"] = (long)TimeCurrent();
   stream_msg["role"]       = g_role;
   stream_msg["source"]     = "ONTRADE_REVERSAL";
   stream_msg["is_reversal"] = true;

   stream_msg["request_action"]       = 1;  // TRADE_ACTION_DEAL
   stream_msg["request_order"]        = (long)0;
   stream_msg["request_symbol"]       = new_pos.symbol;
   stream_msg["request_volume"]       = new_pos.volume;
   stream_msg["request_price"]        = 0.0;
   stream_msg["request_sl"]           = 0.0;
   stream_msg["request_tp"]           = 0.0;
   stream_msg["request_deviation"]    = (long)0;
   // request_type reflete a direção da NOVA perna
   stream_msg["request_type"]         = (int)new_pos.pos_type;
   stream_msg["request_type_filling"] = 0;
   stream_msg["request_comment"]      = "";
   stream_msg["request_position"]     = (long)0;

   stream_msg["result_retcode"] = (long)TRADE_RETCODE_DONE;
   stream_msg["result_deal"]    = (long)0;
   stream_msg["result_order"]   = (long)0;
   stream_msg["result_volume"]  = new_pos.volume;
   stream_msg["result_price"]   = 0.0;
   stream_msg["result_comment"] = "reversal detected by OnTrade";

   // Campos específicos do reversal (Python usa diretamente, sem inferir do DB)
   stream_msg["old_direction"] = (old_pos.pos_type == POSITION_TYPE_BUY) ? "BUY" : "SELL";
   stream_msg["new_direction"] = (new_pos.pos_type == POSITION_TYPE_BUY) ? "BUY" : "SELL";
   stream_msg["old_volume"]    = old_pos.volume;
   stream_msg["new_volume"]    = new_pos.volume;
   stream_msg["position_volume_remaining"] = new_pos.volume;
   stream_msg["position_id"]               = new_pos.position_id;

   if(!SendJsonMessage(stream_msg, "Event"))
      Print("ERROR: Falha ao enviar REVERSAL TRADE_EVENT via EventSocket");

   PrintFormat("OnTrade: REVERSAL detectado (pos_id=%lld, symbol=%s, %s %.2f -> %s %.2f)",
               new_pos.position_id, new_pos.symbol,
               (old_pos.pos_type == POSITION_TYPE_BUY) ? "BUY" : "SELL", old_pos.volume,
               (new_pos.pos_type == POSITION_TYPE_BUY) ? "BUY" : "SELL", new_pos.volume);
}

void EmitSltpModified(const CachedPosition &old_pos, const CachedPosition &new_pos)
{
   JSONNode msg;
   msg["type"]          = "STREAM";
   msg["event"]         = "SLTP_MODIFIED";
   msg["timestamp_mql"] = (long)TimeCurrent();
   msg["role"]          = g_role;
   msg["position_id"]   = new_pos.position_id;
   msg["symbol"]        = new_pos.symbol;
   msg["sl"]            = new_pos.sl;
   msg["tp"]            = new_pos.tp;
   msg["old_sl"]        = old_pos.sl;
   msg["old_tp"]        = old_pos.tp;
   msg["volume"]        = new_pos.volume;

   if(!SendJsonMessage(msg, "Event"))
      Print("ERROR: Falha ao enviar SLTP_MODIFIED via EventSocket");

   PrintFormat("OnTrade: SL/TP modificado (pos_id=%lld, symbol=%s, sl=%.5f→%.5f, tp=%.5f→%.5f)",
               new_pos.position_id, new_pos.symbol,
               old_pos.sl, new_pos.sl, old_pos.tp, new_pos.tp);
}

//+------------------------------------------------------------------+
//| Emite TRADE_EVENT sintético de ABERTURA quando posição nova       |
//| aparece no snapshot. Cobre o cenário B3 (execução assíncrona) em |
//| que OnTradeTransaction dispara antes de DEAL_POSITION_ID /        |
//| ORDER_POSITION_ID estarem preenchidos.                            |
//+------------------------------------------------------------------+
void EmitSyntheticOpenEvent(const CachedPosition &new_pos)
{
   int order_type = (new_pos.pos_type == POSITION_TYPE_BUY) ? 0 : 1;

   JSONNode stream_msg;
   stream_msg["type"]          = "STREAM";
   stream_msg["event"]         = "TRADE_EVENT";
   stream_msg["timestamp_mql"] = (long)TimeCurrent();
   stream_msg["role"]          = g_role;
   stream_msg["source"]        = "ONTRADE_OPEN";

   stream_msg["request_action"]       = 1;  // TRADE_ACTION_DEAL
   stream_msg["request_order"]        = (long)0;
   stream_msg["request_symbol"]       = new_pos.symbol;
   stream_msg["request_volume"]       = new_pos.volume;
   stream_msg["request_price"]        = 0.0;
   stream_msg["request_sl"]           = new_pos.sl;
   stream_msg["request_tp"]           = new_pos.tp;
   stream_msg["request_deviation"]    = (long)0;
   stream_msg["request_type"]         = order_type;  // 0=BUY, 1=SELL
   stream_msg["request_type_filling"] = 0;
   stream_msg["request_comment"]      = "";
   stream_msg["request_position"]     = (long)0;  // 0 = abertura nova

   stream_msg["result_retcode"] = (long)TRADE_RETCODE_DONE;
   stream_msg["result_deal"]    = (long)0;
   stream_msg["result_order"]   = (long)0;
   stream_msg["result_volume"]  = new_pos.volume;
   stream_msg["result_price"]   = 0.0;
   stream_msg["result_comment"] = "open detected by OnTrade";

   stream_msg["position_id"] = new_pos.position_id;

   if(!SendJsonMessage(stream_msg, "Event"))
      Print("ERROR: Falha ao enviar OPEN TRADE_EVENT via OnTrade");

   PrintFormat("OnTrade: NOVA POSIÇÃO detectada (pos_id=%lld, symbol=%s, %s %.2f)",
               new_pos.position_id, new_pos.symbol,
               (new_pos.pos_type == POSITION_TYPE_BUY) ? "BUY" : "SELL", new_pos.volume);
}

//+------------------------------------------------------------------+
//| Emite TRADE_EVENT sintético de ADD quando o volume de uma posição |
//| existente aumenta sem que OnTradeTransaction tenha emitido (ex.:  |
//| position_id veio 0 na execução assíncrona). request_volume é o    |
//| INCREMENTO; o Python classifica como BUY/SELL e, como o slave já  |
//| tem a posição, segue o caminho de ADD.                            |
//+------------------------------------------------------------------+
void EmitSyntheticAddEvent(const CachedPosition &pos, double added_volume)
{
   int order_type = (pos.pos_type == POSITION_TYPE_BUY) ? 0 : 1;

   JSONNode stream_msg;
   stream_msg["type"]          = "STREAM";
   stream_msg["event"]         = "TRADE_EVENT";
   stream_msg["timestamp_mql"] = (long)TimeCurrent();
   stream_msg["role"]          = g_role;
   stream_msg["source"]        = "ONTRADE_ADD";

   stream_msg["request_action"]       = 1;  // TRADE_ACTION_DEAL
   stream_msg["request_order"]        = (long)0;
   stream_msg["request_symbol"]       = pos.symbol;
   stream_msg["request_volume"]       = added_volume;
   stream_msg["request_price"]        = 0.0;
   stream_msg["request_sl"]           = pos.sl;
   stream_msg["request_tp"]           = pos.tp;
   stream_msg["request_deviation"]    = (long)0;
   stream_msg["request_type"]         = order_type;  // 0=BUY, 1=SELL
   stream_msg["request_type_filling"] = 0;
   stream_msg["request_comment"]      = "";
   stream_msg["request_position"]     = (long)0;  // 0 → Python classifica como BUY/SELL

   stream_msg["result_retcode"] = (long)TRADE_RETCODE_DONE;
   stream_msg["result_deal"]    = (long)0;
   stream_msg["result_order"]   = (long)0;
   stream_msg["result_volume"]  = added_volume;
   stream_msg["result_price"]   = 0.0;
   stream_msg["result_comment"] = "add detected by OnTrade";

   stream_msg["position_id"] = pos.position_id;

   if(!SendJsonMessage(stream_msg, "Event"))
      Print("ERROR: Falha ao enviar ADD TRADE_EVENT via OnTrade");

   PrintFormat("OnTrade: ADD detectado (pos_id=%lld, symbol=%s, %s +%.2f)",
               pos.position_id, pos.symbol,
               (pos.pos_type == POSITION_TYPE_BUY) ? "BUY" : "SELL", added_volume);
}

void OnTrade()
{
   if(g_role != "MASTER" || !g_is_connected || g_magic_number == 0)
      return;

   CachedPosition new_snap[MAX_CACHED_POSITIONS];
   int new_count = BuildPositionSnapshot(new_snap);

   for(int i = 0; i < g_pos_cache_size; i++)
   {
      bool found = false;
      for(int j = 0; j < new_count; j++)
      {
         if(g_pos_cache[i].position_id != new_snap[j].position_id)
            continue;

         found = true;

         // pos_type mudou → reversal em netting (ordem oposta com volume > posição atual).
         // Precisa vir ANTES do diff de volume: o volume "diminuiu" visualmente mas a
         // direção virou — tratar como partial close produziria slave em direção oposta.
         if(new_snap[j].pos_type != g_pos_cache[i].pos_type)
         {
            EmitReversalEvent(g_pos_cache[i], new_snap[j]);
            break;
         }

         // Volume diminuiu → partial close externo (mesma direção)
         if(new_snap[j].volume < g_pos_cache[i].volume - 0.000001)
         {
            double closed_vol = g_pos_cache[i].volume - new_snap[j].volume;
            EmitSyntheticTradeEvent(g_pos_cache[i], closed_vol, new_snap[j].volume);
         }
         // Volume aumentou → ADD externo na mesma direção que OnTradeTransaction
         // não emitiu (position_id veio 0 na execução assíncrona). Sem este
         // ramo o ADD do master nunca chegava aos slaves.
         else if(new_snap[j].volume > g_pos_cache[i].volume + 0.000001)
         {
            double added_vol = new_snap[j].volume - g_pos_cache[i].volume;
            EmitSyntheticAddEvent(new_snap[j], added_vol);
         }

         // SL ou TP mudou
         if(MathAbs(new_snap[j].sl - g_pos_cache[i].sl) > 0.000001
            || MathAbs(new_snap[j].tp - g_pos_cache[i].tp) > 0.000001)
         {
            EmitSltpModified(g_pos_cache[i], new_snap[j]);
         }
         break;
      }

      if(!found)
      {
         // Posição desapareceu → fechamento total (SL/TP hit, SO, mobile close, etc.)
         EmitSyntheticTradeEvent(g_pos_cache[i], g_pos_cache[i].volume, 0.0);
      }
   }

   // Detecta ABERTURAS NOVAS: posição em new_snap sem match no cache antigo.
   // Cobre o caso B3 (execução assíncrona) em que OnTradeTransaction não
   // conseguiu resolver position_id e desistiu de emitir. Aqui o snapshot
   // já tem a posição confirmada, então POSITION_IDENTIFIER é certeiro.
   for(int j = 0; j < new_count; j++)
   {
      bool was_cached = false;
      for(int i = 0; i < g_pos_cache_size; i++)
      {
         if(g_pos_cache[i].position_id == new_snap[j].position_id)
         {
            was_cached = true;
            break;
         }
      }
      if(!was_cached)
         EmitSyntheticOpenEvent(new_snap[j]);
   }

   // Atualizar cache
   g_pos_cache_size = new_count;
   for(int i = 0; i < new_count; i++)
   {
      g_pos_cache[i].position_id = new_snap[i].position_id;
      g_pos_cache[i].symbol      = new_snap[i].symbol;
      g_pos_cache[i].volume      = new_snap[i].volume;
      g_pos_cache[i].sl          = new_snap[i].sl;
      g_pos_cache[i].tp          = new_snap[i].tp;
      g_pos_cache[i].pos_type    = new_snap[i].pos_type;
      g_pos_cache[i].magic       = new_snap[i].magic;
   }
}

//+------------------------------------------------------------------+
//| Bloco 5 - OnTradeTransaction                                    |
//| Publica TRADE_EVENT via EventSocket para o Python.               |
//| Ambos MASTER e SLAVE publicam, mas o Python só replica do MASTER.|
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   // ── Caminho 1: DEAL_ADD — detecção de alien trades (SLAVE) ──
   // TRADE_TRANSACTION_REQUEST só dispara no terminal que enviou a ordem.
   // Para detectar operações alienígenas feitas em OUTRO MT5 conectado na mesma conta
   // (ou mobile/webtrader/outro EA), precisamos observar TRADE_TRANSACTION_DEAL_ADD,
   // que chega em todos os terminais quando um deal entra no histórico da conta.
   // DEAL_ADD também dispara no próprio terminal que originou o trade, então
   // esta é a ÚNICA fonte de detecção de alien (evita duplicação).
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
   {
      if(g_role == "SLAVE" && g_magic_number > 0 && trans.deal > 0
         && HistoryDealSelect(trans.deal))
      {
         long deal_magic = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
         long deal_type  = HistoryDealGetInteger(trans.deal, DEAL_TYPE);

         // Só interessa BUY/SELL (ignora BALANCE, CREDIT, CORRECTION, etc.)
         bool is_trade_deal = (deal_type == DEAL_TYPE_BUY || deal_type == DEAL_TYPE_SELL);

         if(is_trade_deal && deal_magic != g_magic_number)
         {
            string symbol   = HistoryDealGetString(trans.deal, DEAL_SYMBOL);
            double volume   = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
            string type_str = (deal_type == DEAL_TYPE_BUY) ? "BUY" : "SELL";

            PrintFormat("ALIEN TRADE detectado! magic=%lld (esperado=%lld), symbol=%s, %s %.2f lotes, deal=%lld",
                        deal_magic, g_magic_number, symbol, type_str, volume, (long)trans.deal);

            JSONNode alien_msg;
            alien_msg["type"] = "STREAM";
            alien_msg["event"] = "ALIEN_TRADE";
            alien_msg["timestamp_mql"] = (long)TimeCurrent();
            alien_msg["role"] = g_role;
            alien_msg["deal"] = (long)trans.deal;
            alien_msg["deal_magic"] = deal_magic;
            alien_msg["expected_magic"] = g_magic_number;
            alien_msg["symbol"] = symbol;
            alien_msg["volume"] = volume;
            alien_msg["deal_type"] = type_str;

            if(!SendJsonMessage(alien_msg, "Event"))
               Print("ERROR: Falha ao enviar ALIEN_TRADE via EventSocket");
         }
      }
      return;
   }

   // ── Caminho 2: REQUEST — resposta async + TRADE_EVENT para replicação master→slave ──
   // Só processa TRADE_TRANSACTION_REQUEST — o único tipo que preenche request/result.
   // Outros tipos (ORDER_ADD, HISTORY_ADD, etc.) chegam com result.retcode==0 e request zerado.
   if(trans.type != TRADE_TRANSACTION_REQUEST)
      return;

   // Ignora retcodes irrelevantes
   if(result.retcode == 0 || result.retcode == TRADE_RETCODE_NO_CHANGES)
      return;

   if(InpDebugLog)
   {
      PrintFormat("OnTradeTransaction - role=%s, action=%s, retcode=%d, deal=%lld, order=%lld, symbol=%s, volume=%.2f",
                  g_role, EnumToString(request.action), result.retcode,
                  result.deal, result.order, request.symbol, request.volume);
   }

   // ── Resposta assíncrona para OrderSendAsync pendentes ──
   // Deve ficar ANTES do filtro de retcode do TRADE_EVENT, pois precisamos responder
   // ao Python para qualquer retcode (sucesso ou erro).
   if(result.request_id > 0)
   {
      string tcp_id = FindAndRemovePendingRequest((ulong)result.request_id);
      if(tcp_id != "")
      {
         JSONNode async_response;
         async_response["type"] = "RESPONSE";
         async_response["request_id"] = tcp_id;

         if(result.retcode == TRADE_RETCODE_DONE || result.retcode == TRADE_RETCODE_PLACED
            || result.retcode == TRADE_RETCODE_DONE_PARTIAL)
         {
            async_response["status"] = "OK";
         }
         else
         {
            async_response["status"] = "ERROR";
            async_response["error_message"] = StringFormat("Trade retcode=%d: %s", result.retcode, result.comment);
         }

         async_response["retcode"] = (long)result.retcode;
         async_response["result"] = result.comment;
         async_response["deal"] = (long)result.deal;
         async_response["order"] = (long)result.order;
         async_response["volume"] = result.volume;
         async_response["price"] = result.price;

         // ticket: posição (para close) ou deal (para abertura)
         if(request.position > 0)
            async_response["ticket"] = (long)request.position;
         else
            async_response["ticket"] = (long)result.deal;

         SendJsonMessage(async_response, "Command");

         if(InpDebugLog)
            PrintFormat("Async RESPONSE enviado: tcp_req=%s, retcode=%d, deal=%lld",
                        tcp_id, result.retcode, result.deal);
      }
   }

   // Bufferiza emissão de TRADE_EVENT até receber SET_MAGIC_NUMBER do Python.
   // Trades que ocorrerem nessa janela (REGISTER → SET_MAGIC_NUMBER, geralmente
   // ms) serão recuperados pelo snapshot diff do OnTrade na primeira execução
   // após g_magic_number > 0 — o cache de posições ainda reflete o estado da
   // inicialização (RefreshPositionCache em OnInit), então o diff identifica
   // o trade como abertura nova. A resposta async acima já fluiu — necessária
   // para não deixar Python esperando comando.
   if(g_magic_number == 0)
   {
      if(InpDebugLog)
         Print("TRADE_EVENT bufferizado: SET_MAGIC_NUMBER ainda não recebido");
      return;
   }

   // Só envia para retcodes relevantes
   if(result.retcode != TRADE_RETCODE_DONE &&
      result.retcode != TRADE_RETCODE_REJECT &&
      result.retcode != TRADE_RETCODE_INVALID &&
      result.retcode != TRADE_RETCODE_INVALID_PRICE)
   {
      if(InpDebugLog)
         PrintFormat("Não enviando TRADE_EVENT para retcode=%d", result.retcode);
      return;
   }

   JSONNode stream_msg;
   stream_msg["type"] = "STREAM";
   stream_msg["event"] = "TRADE_EVENT";
   stream_msg["timestamp_mql"] = (long)TimeCurrent();
   stream_msg["role"] = g_role;

   // Request data - FLATTENAR para contornar bug do Copy() no Json.mqh
   // (Copy() sobrescreve m_key com "" ao atribuir JSONNode via operator=)
   stream_msg["request_action"] = (int)request.action;
   stream_msg["request_order"] = (long)request.order;
   stream_msg["request_symbol"] = request.symbol;
   stream_msg["request_volume"] = request.volume;
   stream_msg["request_price"] = request.price;
   stream_msg["request_sl"] = request.sl;
   stream_msg["request_tp"] = request.tp;
   stream_msg["request_deviation"] = (long)request.deviation;
   stream_msg["request_type"] = (int)request.type;
   stream_msg["request_type_filling"] = (int)request.type_filling;
   stream_msg["request_comment"] = request.comment;
   stream_msg["request_position"] = (long)request.position;

   // Result data - FLATTENAR para contornar bug do Copy() no Json.mqh
   stream_msg["result_retcode"] = (long)result.retcode;
   stream_msg["result_deal"] = (long)result.deal;
   stream_msg["result_order"] = (long)result.order;
   stream_msg["result_volume"] = result.volume;
   stream_msg["result_price"] = result.price;
   stream_msg["result_comment"] = result.comment;

   // Dados extras para copytrade
   // POSITION_IDENTIFIER é a chave universal que conecta abertura, parcial e fechamento.
   // Nunca muda, mesmo em NETTING com adição de volume ou reversão de posição.
   // Fonte primária: DEAL_POSITION_ID do histórico — é o único campo 100% consistente
   // em todos os cenários (abertura, parcial, fechamento total, mesmo após posição encerrada).
   long position_id = 0;
   if(request.action == TRADE_ACTION_DEAL)
   {
      // 1ª tentativa: DEAL_POSITION_ID via histórico — método mais confiável
      if(result.deal > 0 && HistoryDealSelect(result.deal))
      {
         position_id = HistoryDealGetInteger(result.deal, DEAL_POSITION_ID);
      }

      // 2ª tentativa: POSITION_IDENTIFIER via posição ativa (abertura ou fechamento parcial)
      if(position_id == 0)
      {
         if(request.position > 0 && PositionSelectByTicket(request.position))
         {
            position_id = PositionGetInteger(POSITION_IDENTIFIER);
         }
         else if(request.position == 0 && PositionSelect(request.symbol))
         {
            position_id = PositionGetInteger(POSITION_IDENTIFIER);
         }
      }

      // 3ª tentativa: ORDER_POSITION_ID via histórico da ordem.
      // Cobre o caso de conta real onde result.deal chega zero (execução assíncrona na bolsa):
      // a ordem já existe no histórico com ORDER_POSITION_ID preenchido mesmo sem deal confirmado.
      if(position_id == 0 && result.order > 0 && HistoryOrderSelect(result.order))
      {
         position_id = HistoryOrderGetInteger(result.order, ORDER_POSITION_ID);
      }

      if(position_id == 0 && InpDebugLog)
         PrintFormat("WARNING: Não foi possível obter POSITION_IDENTIFIER para %s (deal=%lld, pos=%lld)",
                     request.symbol, result.deal, request.position);

      // Volume restante após fechamento (só em fechamentos)
      if(request.position > 0)
      {
         if(PositionSelectByTicket(request.position))
         {
            stream_msg["position_volume_remaining"] = PositionGetDouble(POSITION_VOLUME);
         }
         else
         {
            // Posição não existe mais = fechamento total
            stream_msg["position_volume_remaining"] = 0.0;
         }
      }
   }
   stream_msg["position_id"] = position_id;

   // Sem position_id em uma abertura/ADD (action=DEAL, position=0): em B3 com
   // execução assíncrona, deal/order podem não ter sido confirmados ainda.
   // Não emite o TRADE_EVENT incompleto — OnTrade vai detectar a posição
   // nova via snapshot diff e emitir EmitSyntheticOpenEvent com pos_id certo.
   // O cache NÃO é atualizado nesse caminho de saída — assim OnTrade vê o diff.
   if(position_id == 0 && request.action == TRADE_ACTION_DEAL && request.position == 0)
   {
      if(InpDebugLog)
         PrintFormat("INFO: position_id=0 em OnTradeTransaction (deal=%lld); OnTrade vai resolver.",
                     (long)result.deal);
      return;
   }

   if(!SendJsonMessage(stream_msg, "Event"))
   {
      Print("ERROR: Falha ao enviar TRADE_EVENT via EventSocket");
   }

   // Atualizar cache após emitir TRADE_EVENT (dedup: OnTrade() não verá este diff)
   if(request.action == TRADE_ACTION_DEAL)
      RefreshPositionCache();

   // Nota: a detecção de ALIEN_TRADE foi movida para o caminho DEAL_ADD no topo desta
   // função. DEAL_ADD é a única fonte que dispara em TODOS os terminais da mesma conta
   // (inclusive quando o trade é feito em outro MT5, mobile, webtrader, outro EA, etc.).
}
