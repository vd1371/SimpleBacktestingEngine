import platform
import time
import pprint
import pandas as pd
import logging
import multiprocessing as mp
from multiprocessing import Process, Manager
from collections import deque

from config import LONG, SHORT

from .SimulatorUtils import close_trade_for_stop_loss
from .SimulatorUtils import close_trade_for_take_profit
from .SimulatorUtils import close_trade_at
from .SimulatorUtils import Trade

from src.Simulator.DataProviders import combine_data_add_signal
from src.TradingTools import set_stop_loss_and_take_profit_thresholds


simulation_logger = logging.getLogger("simulation_logger")

def run_alpha_strategies(
    stock_histories,
    trade_history_holder,
    **params
    ):

    if trade_history_holder.is_loaded:
        return
    
    should_log = True
    run_parallel = params["run_parallel"]

    if should_log:
        simulation_logger.info(str(pprint.pformat(params)))
    
    all_market_stocks_dfs = list(market_and_df_interator(
        stock_histories, **params
        ))
    

    # -----------------------------------------------
    # The linear way
    # -----------------------------------------------
    if not run_parallel:
        _run_alpha_strategy_process(
            all_market_stocks_dfs,
            "Main",
            params,
            trade_history_holder,
            None, # results_queue
            None, # processes_alive
        )
        return

    n_cores = min(int(mp.cpu_count() * 0.80), len(stock_histories.data))
    
    with Manager() as manager:
        # results_queue = Queue()
        market_stock_df_queues = manager.dict()
        results_queues = manager.dict()
        processes_alive = manager.dict()
        for i in range (n_cores):
            processes_alive[i] = 0
            market_stock_df_queues[i] = manager.Queue()

        for i, macro_and_other_data in enumerate(all_market_stocks_dfs):
            process_number = i % n_cores
            
            market_stock_df_queues[process_number].put((macro_and_other_data))
            results_queues[process_number] = manager.Queue()

        pool_of_workers = []
        for process_number in range (n_cores):

            # Creating and Adding the process
            worker = Process(
                target = _run_alpha_strategy_process,
                args = (
                    market_stock_df_queues[process_number],
                    process_number,
                    params,
                    None,
                    results_queues[process_number],
                    processes_alive,
                    )
                )
            worker.start()
            pool_of_workers.append(worker)

        print('--- Workers started')
        # Getting the results from the queue
        while any(worker.is_alive() for worker in pool_of_workers):
            time.sleep(5)
            for process_number, results_queue in results_queues.items():
                while not results_queue.empty():
                    res = results_queue.get()
                    if res is not None:
                        trades, market = res
                        trade_history_holder.add_many(trades, market)

        print ("--- Workers tying to join...")
        # Joining the processes
        for worker in pool_of_workers:
            worker.join()

# ---------------------------------------------------------------------------- #
#                                                                              #
#                       The run alpha strategy process                         #
#                                                                              # 
# ---------------------------------------------------------------------------- #
def _run_alpha_strategy_process(
        all_market_stocks_dfs_chunk,
        process_number,
        params,
        trade_history_holder,
        results_queue,
        processes_alive,
    ):

    market = params['market']
    should_log = params["should_log"]

    start = time.time()

    # the linear way
    if isinstance(all_market_stocks_dfs_chunk, list):
        l = len(all_market_stocks_dfs_chunk)
        for i, macro_and_other_data in enumerate(all_market_stocks_dfs_chunk):
            results = run_alpha_strategy_for_one_symbol(macro_and_other_data, params)

            trade_history_holder.add_many(results, market)

            if should_log:
                print (
                    f"Simulating {macro_and_other_data['symbol']} in {market} market. " + \
                    f"{i+1}/{l} in {time.time()-start:.2f}. " + \
                    f"Process number: {process_number}"
                    )
                start = time.time()

    # the parallel way
    else:
        processes_alive[process_number] = 1

        while True:
            time.sleep(1)
            if all(value == 1 for value in processes_alive.values()):
                # print ("All processes are alive")
                break
        
        while not all_market_stocks_dfs_chunk.empty():
            macro_and_other_data = all_market_stocks_dfs_chunk.get()
            results = run_alpha_strategy_for_one_symbol(macro_and_other_data, params)

            results_queue.put((results, market))

            if should_log:
                print (
                    f"Simulating {macro_and_other_data['symbol']} in {market} market. " + \
                    (f"{all_market_stocks_dfs_chunk.qsize()} remaining. T: {time.time()-start:.2f}. " if platform.system() != "Darwin" else "") + \
                    f"Process number: {process_number}"
                    )
                start = time.time()


def run_alpha_strategy_for_one_symbol(macro_and_other_data, params):

    symbol = macro_and_other_data['symbol']

    market = params['market']
    should_close_at_end_of_candle = params['should_close_at_end_of_candle']

    should_limit_one_position_in_run_alpha = params['should_limit_one_position_in_run_alpha']

    df = combine_data_add_signal(macro_and_other_data, **params)

    any_trade_open = False
    latest_trade_closing_time = None

    holder = deque()

    dict_of_signals = df[df['signal'] != 0][
        ['Open', 'High', 'Low', 'Close', 'signal',
            'trade_opening_price', 'Close_t_1',
        ]
    ].to_dict(orient='index')

    dict_of_prices = df[
        ['Open', 'High', 'Low', 'Close', 'Close_t_1']
        ].to_dict(orient='index')
    
    df_for_monitoring = df[[
        'Open', 'High', 'Low', 'Close',
        'Close_t_1', 'end_of_candle',
        'close_long_signal', 'close_short_signal']].copy()

    for t in dict_of_signals:

        if should_limit_one_position_in_run_alpha and \
            latest_trade_closing_time is not None and \
            t <= latest_trade_closing_time:
            continue

        open_price = dict_of_signals[t]['Open']
        close_price = dict_of_signals[t]['Close']
        signal = dict_of_signals[t]['signal']
        trade_opening_price = dict_of_signals[t]['trade_opening_price']
        previous_close_price = dict_of_signals[t]['Close_t_1']

        exact_opening_time = t

        
        # Open a trade if no trade is open on the symbol
        if not any_trade_open and \
            (signal == LONG or signal == SHORT) and \
            not pd.isna(previous_close_price):


            # Open a new trade with the classis way of opening
            trade = Trade(
                    symbol = symbol,
                    opening_time = t,
                    exact_opening_time = exact_opening_time,
                    opening_price = trade_opening_price,
                    market = market,
                    trade_direction= signal,
                )

            stop_loss_threshold, take_profit_threshold = \
                set_stop_loss_and_take_profit_thresholds(
                    trade_opening_price,
                    signal,
                    **params
                )

            # Adding all the info that starts with "stat_", these info
            # will be saved in final reports
            for col in df.columns:
                if col.startswith("stat_"):
                    trade.set_attribute(col, df.loc[t, col])

            any_trade_open = True

        # It's for the cases when no trade is opened
        if not any_trade_open:
            continue
        
        # IMPORTANT: We need to filter the df_for_monitoring based on the t
        # If we close at the end of day, we need to filter the df_for_monitoring for only 1 day
        if should_close_at_end_of_candle:
            # Get the integer index of t
            t_index = df.index.get_loc(t)
            df_monitoring = df_for_monitoring.iloc[t_index: t_index + 300, :]
        else:
            df_monitoring = df_for_monitoring[df_for_monitoring.index >= t]
        t_monitoring = df_monitoring.index[0]

        while True:
            # When the trade is closed, there's no need to iterate until end
            if (trade.is_closed or (trade is None or not any_trade_open)):
                break

            event_type, t_monitoring, _ = _get_next_event(
                df_monitoring, t_monitoring,
                trade,
                stop_loss_threshold, take_profit_threshold,
                **params
                )
            
            # This is for double check. The iteration should start from the
            # candle that corresponds to the opening time
            if t_monitoring < trade.exact_opening_time: continue

            open_price = dict_of_prices[t_monitoring]['Open']
            close_price = dict_of_prices[t_monitoring]['Close']
            previous_close_price = dict_of_prices[t_monitoring]['Close_t_1']
                
            if event_type == "stop_loss_at_open":
                close_trade_for_stop_loss(
                    trade, t_monitoring, open_price,
                    "Stop Loss at Open",
                    **params)

            elif event_type == "take_profit_at_open":
                stop_loss_threshold, take_profit_threshold = \
                    close_trade_for_take_profit(
                        trade,
                        t_monitoring,
                        stop_loss_threshold,
                        open_price,
                        reason = "Take Profit at Open",
                        tocutched_price = open_price,
                        **params
                    )

            elif event_type == "stop_loss_in_candle": 
                close_trade_for_stop_loss(
                    trade, t_monitoring, stop_loss_threshold,
                    reason = "Stop loss in candle",
                    **params)
            

            elif event_type == "take_profit_in_candle":
                
                stop_loss_threshold, take_profit_threshold = \
                    close_trade_for_take_profit(
                        trade,
                        t_monitoring,
                        stop_loss_threshold,
                        take_profit_threshold,
                        reason = "Take Profit in candle",
                        **params
                    )

            elif event_type == "stop_loss_at_close":
                close_trade_at(trade, t_monitoring, close_price, "Stop loss at daily close", **params)

            elif event_type == "take_profit_at_close":
                stop_loss_threshold, take_profit_threshold = \
                    close_trade_for_take_profit(
                        trade,
                        t_monitoring,
                        stop_loss_threshold,
                        take_profit_threshold,
                        reason = "Take Profit at Close",
                        touched_price = close_price,
                        **params
                    )

            elif event_type == "hitting_close_signal":
                close_trade_at(trade, t_monitoring, close_price, "Hitting Close Signal", **params)

            elif event_type == "max_holding_days_touched":
                close_trade_at(trade, t_monitoring, close_price, "Max holding days touched", **params)

            elif event_type == "close_at_end_of_candle":
                close_trade_at(trade, t_monitoring, close_price, "Close at the end of day", **params)
            
            # Last candle
            elif event_type == "last_candle":
                close_trade_at(trade, t_monitoring, close_price, "Close at last candle of data", **params)

            # Going to the next candle
            next_candles_times = df_monitoring[df_monitoring.index > t_monitoring].index
            if len(next_candles_times) > 0:
                t_monitoring = next_candles_times[0]

            else:
                break

        try:
            if any_trade_open and trade.is_closed:
                
                holder.append(trade)

                any_trade_open = False
                latest_trade_closing_time = trade.closing_time
                trade = None

        except UnboundLocalError:
            pass
    
    return list(holder)



def market_and_df_interator(stock_histories, **params):

    for symbol in stock_histories.data:
        yield stock_histories.get(symbol)



def _get_next_event(
    df_in, t_monitoring,
    trade,
    stop_loss_threshold, take_profit_threshold,
    **params):

    should_stop_loss = params['should_stop_loss']
    should_take_profit = params['should_take_profit']
    should_close_at_end_of_candle = params['should_close_at_end_of_candle']
    max_holding_days = 180

    events = deque()

    df = df_in[
        (df_in.index >= t_monitoring) &
        (df_in.index <= t_monitoring + pd.Timedelta(days=max_holding_days+10))
        ]

    not_same_candle_open_at_open = df.index != trade.exact_opening_time

    # SL at Open
    if params['should_stop_loss']:
        filtered_df_for_sl_at_open = df[
            ((trade.trade_direction*(df['Open']-stop_loss_threshold)<0) &
            (not_same_candle_open_at_open))
            ]
        if len(filtered_df_for_sl_at_open) > 0:
            events.append(('stop_loss_at_open', filtered_df_for_sl_at_open.index[0], priorities_of_events['stop_loss_at_open']))

    # TP at open
    if should_take_profit:
        filtered_df_for_tp_at_open = df[
            ((trade.trade_direction*(df['Open']-take_profit_threshold)>0) &
            (not_same_candle_open_at_open))
            ]

        if len(filtered_df_for_tp_at_open) > 0:
            events.append(('take_profit_at_open', filtered_df_for_tp_at_open.index[0], priorities_of_events['take_profit_at_open']))

    
    # This is for the case when we only check at daily close
    if should_stop_loss and trade.trade_direction == LONG:
        filtered_df_for_sl_in_candle = df[df['Close'] < stop_loss_threshold]

        if len(filtered_df_for_sl_in_candle) > 0:
            events.append(('stop_loss_at_close', filtered_df_for_sl_in_candle.index[0], priorities_of_events['stop_loss_at_close']))
    
    elif should_stop_loss and trade.trade_direction == SHORT:
        filtered_df_for_sl_in_candle = df[df['Close'] > stop_loss_threshold]

        if len(filtered_df_for_sl_in_candle) > 0:
            events.append(('stop_loss_at_close', filtered_df_for_sl_in_candle.index[0], priorities_of_events['stop_loss_at_close']))

    # TP in candle
    if should_take_profit and trade.trade_direction == LONG:
        filtered_df_for_tp_in_candle = df[(df['Close'] > take_profit_threshold) & (not_same_candle_open_at_open)]
        if len(filtered_df_for_tp_in_candle) > 0:
            events.append(('take_profit_at_close', filtered_df_for_tp_in_candle.index[0], priorities_of_events['take_profit_at_close']))

    elif should_take_profit and trade.trade_direction == SHORT:
        filtered_df_for_tp_in_candle = df[(df['Close'] < take_profit_threshold) & (not_same_candle_open_at_open)]

        if len(filtered_df_for_tp_in_candle) > 0:
            events.append(('take_profit_at_close', filtered_df_for_tp_in_candle.index[0], priorities_of_events['take_profit_at_close']))

    # Max holding event
    filtered_df_for_max_holding = df[(df.index - trade.opening_time).days >= max_holding_days]
    if len(filtered_df_for_max_holding) > 0:
        events.append(('max_holding_days_touched', filtered_df_for_max_holding.index[0], priorities_of_events['max_holding_days_touched']))

    # Close at end of day
    if should_close_at_end_of_candle:

        filtered_df_for_end_of_candle = df[df['end_of_candle'] == 1]
        if len(filtered_df_for_end_of_candle) > 0:
            events.append(('close_at_end_of_candle', filtered_df_for_end_of_candle.index[0], priorities_of_events['close_at_end_of_candle']))
    
    # Last candle of data
    last_candle_time = df.index[-1]
    event = 'last_candle'
    events.append((event, last_candle_time, priorities_of_events[event]))

    # Sort based on the time and priority
    sorted_events = sorted(events, key=lambda x: (x[1], -x[2]))
    
    return sorted_events[0]
    
priorities_of_events = {
    'stop_loss_at_open': 90,
    'take_profit_at_open': 80,
    'stop_loss_in_candle': 70,
    'take_profit_in_candle': 60,
    'take_profit_at_close': 50,
    'stop_loss_at_close': 50,
    'hitting_close_signal': 50,
    'max_holding_days_touched': 11,
    'close_at_end_of_candle': 12,
    'last_candle': 10
}