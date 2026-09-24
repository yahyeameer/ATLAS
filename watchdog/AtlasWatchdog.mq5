//+------------------------------------------------------------------+
//| AtlasWatchdog.mq5                                                  |
//| ATLAS broker-side backstop (PRD §12 layer 3, §23).                |
//|                                                                    |
//| Runs inside the MT5 terminal, independent of the Python engine.   |
//| - Flattens the account and latches when equity reaches its own    |
//|   hard lines, which sit between the engine's KILL lines and the   |
//|   firm's floors. It keeps closing anything that opens while       |
//|   latched.                                                         |
//| - Writes a heartbeat line every few seconds to Common\Files. The  |
//|   engine reads it; 60 s of silence puts the engine in HALT, and a |
//|   FLATTENED state becomes an engine kill.                          |
//| Firm floors come from the engine's atlas_limits.txt while it is   |
//| fresh (written each engine step for the current server day).      |
//| When the engine is down, the EA uses its own estimate: daily loss |
//| from the balance at server midnight, max loss static from the     |
//| initial balance.                                                   |
//|                                                                    |
//| Reset after a flatten: the operator deletes the terminal global   |
//| variable ATLAS_WD_FLATTENED (F3) and re-attaches the EA.          |
//|                                                                    |
//| NOT YET COMPILED: MetaEditor is Windows-only. Compile and run it  |
//| on the demo terminal before T4 counts as done (docs/t4-...).      |
//+------------------------------------------------------------------+
#property copyright "ATLAS"
#property version   "1.00"
#property description "ATLAS watchdog: flatten on hard equity breach, heartbeat for the engine"

#include <Trade\Trade.mqh>

input double InpInitialBalance    = 10000.0;   // account initial balance (config/atlas.yaml)
input double InpDailyLossPct      = 5.0;       // firm daily loss, % of initial (prop_rules)
input double InpMaxLossPct        = 10.0;      // firm max loss, % of initial (prop_rules)
input double InpDailyHardShare    = 0.85;      // flatten at this share of the firm daily loss (engine KILLs at 0.75)
input double InpMaxLossHardShare  = 0.80;      // flatten at this share of the firm max loss (engine KILLs at 0.60)
input bool   InpCloseAllPositions = true;      // dedicated account: close every position, not only ATLAS's
input long   InpMagicBase         = 26090000;  // execution.magic_base; ATLAS magics are base..base+999
input string InpHeartbeatFile     = "atlas_watchdog.txt";
input string InpLimitsFile        = "atlas_limits.txt";
input int    InpLimitsMaxAgeSec   = 93600;     // ignore a limits file older than 26 h
input int    InpHeartbeatSeconds  = 5;

CTrade   g_trade;
bool     g_flattened       = false;
datetime g_day             = 0;
double   g_dayStartBalance = 0.0;
datetime g_lastBeat        = 0;

int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagicBase + 999);
   g_trade.SetDeviationInPoints(50);
   g_flattened = GlobalVariableCheck("ATLAS_WD_FLATTENED") && GlobalVariableGet("ATLAS_WD_FLATTENED") > 0.0;
   RollDay();
   EventSetTimer(1);
   Beat();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTick()
{
   Check();
}

void OnTimer()
{
   Check();
   if(TimeGMT() - g_lastBeat >= InpHeartbeatSeconds)
      Beat();
}

datetime ServerDay(const datetime t)
{
   return(t - (t % 86400));
}

void RollDay()
{
   datetime d = ServerDay(TimeTradeServer());
   if(d != g_day)
   {
      g_day = d;
      g_dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   }
}

// ATLAS-LIMITS 1 <server date YYYY.MM.DD> <daily_floor> <daily_amt> <max_floor> <max_amt> <utc seconds>
bool ReadLimits(double &dailyFloor, double &dailyAmt, double &maxFloor, double &maxAmt)
{
   int h = FileOpen(InpLimitsFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      return(false);
   string line = FileReadString(h);
   FileClose(h);
   string parts[];
   if(StringSplit(line, ' ', parts) < 8 || parts[0] != "ATLAS-LIMITS")
      return(false);
   if(StringToTime(parts[2]) != g_day)
      return(false);
   if(TimeGMT() - (datetime)StringToInteger(parts[7]) > InpLimitsMaxAgeSec)
      return(false);
   dailyFloor = StringToDouble(parts[3]);
   dailyAmt   = StringToDouble(parts[4]);
   maxFloor   = StringToDouble(parts[5]);
   maxAmt     = StringToDouble(parts[6]);
   return(dailyAmt > 0.0 && maxAmt > 0.0);
}

void Check()
{
   RollDay();
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double dailyFloor = 0.0, dailyAmt = 0.0, maxFloor = 0.0, maxAmt = 0.0;
   if(!ReadLimits(dailyFloor, dailyAmt, maxFloor, maxAmt))
   {
      dailyAmt   = InpInitialBalance * InpDailyLossPct / 100.0;
      dailyFloor = g_dayStartBalance - dailyAmt;
      maxAmt     = InpInitialBalance * InpMaxLossPct / 100.0;
      maxFloor   = InpInitialBalance - maxAmt;
   }
   double dailyLine = dailyFloor + (1.0 - InpDailyHardShare) * dailyAmt;
   double maxLine   = maxFloor + (1.0 - InpMaxLossHardShare) * maxAmt;

   if(!g_flattened && (equity <= dailyLine || equity <= maxLine))
   {
      g_flattened = true;
      GlobalVariableSet("ATLAS_WD_FLATTENED", 1.0);
      PrintFormat("ATLAS watchdog: equity %.2f reached a hard line (daily %.2f, max loss %.2f); flattening",
                  equity, dailyLine, maxLine);
      Beat();
   }
   if(g_flattened)
      FlattenAll();
}

bool IsAtlas(const long magic, const string comment)
{
   return((magic >= InpMagicBase && magic < InpMagicBase + 1000) || StringFind(comment, "ATL-") == 0);
}

void FlattenAll()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!InpCloseAllPositions && !IsAtlas(PositionGetInteger(POSITION_MAGIC), PositionGetString(POSITION_COMMENT)))
         continue;
      if(!g_trade.PositionClose(ticket))
         PrintFormat("ATLAS watchdog: close %I64u failed, retcode %u", ticket, g_trade.ResultRetcode());
   }
   for(int j = OrdersTotal() - 1; j >= 0; j--)
   {
      ulong order = OrderGetTicket(j);
      if(order == 0)
         continue;
      if(!InpCloseAllPositions && !IsAtlas(OrderGetInteger(ORDER_MAGIC), OrderGetString(ORDER_COMMENT)))
         continue;
      g_trade.OrderDelete(order);
   }
}

// ATLAS-WD 1 <utc seconds> <equity> <OK|FLATTENED>
void Beat()
{
   int h = FileOpen(InpHeartbeatFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ);
   if(h == INVALID_HANDLE)
      return;
   string line = "ATLAS-WD 1 " + IntegerToString((long)TimeGMT()) + " " +
                 DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + " " + (g_flattened ? "FLATTENED" : "OK");
   FileWriteString(h, line + "\r\n");
   FileClose(h);
   g_lastBeat = TimeGMT();
}
//+------------------------------------------------------------------+
