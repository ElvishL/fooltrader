# -*- coding: utf-8 -*-

import logging
import math

from fooltrader.api.esapi import esapi
from fooltrader.api.esapi.esapi import es_get_sim_account
from fooltrader.domain.business.es_account import SimAccount, Position
from fooltrader.trader.common import TradingSignalType
from fooltrader.utils.es_utils import es_get_latest_record, es_delete, es_index_mapping
from fooltrader.utils.time_utils import to_pd_timestamp
from fooltrader.utils.utils import fill_doc_type

ORDER_TYPE_LONG = 0
ORDER_TYPE_SHORT = 1
ORDER_TYPE_CLOSE_LONG = 2
ORDER_TYPE_CLOSE_SHORT = 3


class AccountService(object):
    def handle_trading_signal(self, trading_signal):
        pass


class SimAccountService(AccountService):
    def __init__(self, trader_name,
                 model_name,
                 timestamp,
                 base_capital=1000000,
                 buy_cost=0.001,
                 sell_cost=0.001,
                 slippage=0.001):
        self.logger = logging.getLogger(__name__)

        self.base_capital = base_capital
        self.buy_cost = buy_cost
        self.sell_cost = sell_cost
        self.slippage = slippage
        self.trader_name = trader_name
        self.model_name = model_name

        account = es_get_latest_record(index='sim_account', query={"term": {"traderName": trader_name}})

        if account:
            self.logger.warning("trader:{} has run before,old result would be deleted".format(trader_name))
            es_delete(index='sim_account', query={"term": {"traderName": trader_name}})

        es_index_mapping('sim_account', SimAccount)

        self.account = SimAccount()
        self.account.traderName = trader_name
        self.account.modelName = model_name
        self.account.cash = self.base_capital
        self.account.positions = []
        self.account.value = self.base_capital
        self.account.timestamp = timestamp
        self.account.save()

    def handle_trading_signal(self, trading_signal):
        if trading_signal:
            if trading_signal.trading_signal_type == TradingSignalType.TRADING_SIGNAl_LONG:
                try:
                    self.close_short(security_id=trading_signal.security_id, current_price=trading_signal.current_price,
                                     current_timestamp=trading_signal.start_timestamp)
                except Exception as e:
                    pass
                try:
                    self.buy(security_id=trading_signal.security_id, current_price=trading_signal.current_price,
                             current_timestamp=trading_signal.start_timestamp)
                except Exception as e:
                    pass

            if trading_signal.trading_signal_type == TradingSignalType.TRADING_SIGNAl_SHORT:
                try:
                    self.close_long(security_id=trading_signal.security_id, current_price=trading_signal.current_price,
                                    current_timestamp=trading_signal.start_timestamp)
                except Exception as e:
                    pass
                try:
                    self.sell(security_id=trading_signal.security_id, current_price=trading_signal.current_price,
                              current_timestamp=trading_signal.start_timestamp)
                except Exception as e:
                    pass

    def get_account(self, refresh=True):
        if refresh:
            account_json = es_get_sim_account(trader_name=self.trader_name, model_name=self.model_name, size=1)['data'][
                0]
            self.account = SimAccount()
            fill_doc_type(self.account, account_json)

        return self.account

    def get_current_position(self, security_id):
        account = self.get_account()
        for position in account.positions:
            if position.securityId == security_id:
                return position
        return Position(security_id=security_id)

    # 计算收盘账户
    def calculate_closing_account(self, the_date):
        account = self.get_account()
        for position in account.positions:
            kdata = esapi.es_get_kdata(security_item=position['securityId'], the_date=the_date)
            closing_price = kdata['close']
            position.availableLong = position.longAmount
            position.availableShort = position.shortAmount

            # 做多导致的市值变化体现了value里
            # 做空导致的市值变化体现在value和cash里
            position.value = position.longAmount * closing_price + position.shortAmount * closing_price

            account.cash += 2 * (position.shortAmount * (position.averageShortPrice - closing_price))

            position.averageShortPrice = closing_price
            position.averageLongPrice = closing_price

    # 两种情况下会被调用：
    # 1)操作导致账户更新
    # 2)当日收盘
    def save_account(self, timestamp):
        self.account.timestamp = timestamp
        self.account.save()

    def update_account(self, security_id, new_position, timestamp):
        # 先去掉之前的position
        positions = [position for position in self.account.positions if position.securityId != security_id]
        # 更新为新的position
        positions.append(new_position)
        self.account.positions = positions
        self.account.timestamp = to_pd_timestamp(timestamp)
        self.account.save()

    def update_position(self, current_position, order_amount, current_price, order_type):
        if order_type == ORDER_TYPE_LONG:
            # 计算平均价
            long_amount = current_position.longAmount + order_amount
            current_position.averageLongPrice = (current_position.averageLongPrice *
                                                 current_position.longAmount + current_price * order_amount) / long_amount

            current_position.longAmount = long_amount

            if current_position.tradingT == 0:
                current_position.availableLong += order_amount
        elif order_type == ORDER_TYPE_SHORT:
            short_amount = current_position.shortAmount + order_amount
            current_position.averageShortPrice = (current_position.averageShortPrice *
                                                  current_position.shortAmount + current_price * order_amount) / short_amount

            current_position.shortAmount = short_amount

            if current_position.tradingT == 0:
                current_position.availableShort += order_amount

    # 开多,对于某些品种只能开多，比如中国股票
    def buy(self, security_id, current_price, current_timestamp, order_amount=0, order_pct=1.0, order_price=0):
        self.order(security_id, current_price, current_timestamp, order_amount, order_pct, order_price,
                   order_type=ORDER_TYPE_LONG)

    # 开空
    def sell(self, security_id, current_price, current_timestamp, order_amount=0, order_pct=1.0, order_price=0):
        self.order(security_id, current_price, current_timestamp, order_amount, order_pct, order_price,
                   order_type=ORDER_TYPE_SHORT)

    # 平多
    def close_long(self, security_id, current_price, current_timestamp, order_amount=0, order_pct=1.0, order_price=0):
        self.order(security_id, current_price, current_timestamp, order_amount, order_pct, order_price,
                   order_type=ORDER_TYPE_CLOSE_LONG)

    # 平空
    def close_short(self, security_id, current_price, current_timestamp, order_amount=0, order_pct=1.0, order_price=0):
        self.order(security_id, current_price, current_timestamp, order_amount, order_pct, order_price,
                   order_type=ORDER_TYPE_CLOSE_SHORT)

    def order(self, security_id, current_price, current_timestamp, order_amount=0, order_pct=1.0, order_price=0,
              order_type=ORDER_TYPE_LONG):
        """
        对每个投资标的开多.

        Parameters
        ----------
        security_id : str
            交易标的id

        current_price : float
            当前价格

        order_amount : int
            数量

        order_pct : float
            使用可用现金的百分比,0.0-1.0

        order_price : float
            用于限价交易

        order_type : {ORDER_TYPE_LONG,ORDER_TYPE_SHORT,ORDER_TYPE_CLOSE_LONG,ORDER_TYPE_CLOSE_SHORT}
            交易类型

        Returns
        -------


        """

        # 市价交易,就是买卖是"当时"并"一定"能成交的
        # 简单起见，目前只支持这种方式
        if order_price == 0:
            current_position = self.get_current_position(security_id=security_id)

            # 按数量交易
            if order_amount > 0:
                # 开多
                if order_type == ORDER_TYPE_LONG:
                    need_money = (order_amount * current_price) * (1 + self.slippage + self.buy_cost)
                    if self.account.cash >= need_money:
                        self.account.cash -= need_money
                        self.update_position(current_position, order_amount, current_price, order_type)
                    else:
                        raise Exception("not enough money")
                # 开空
                elif order_type == ORDER_TYPE_SHORT:
                    need_money = (order_amount * current_price) * (1 + self.slippage + self.buy_cost)
                    if self.account.cash >= need_money:
                        self.account.cash -= need_money
                        self.update_position(current_position, order_amount, current_price, order_type)
                    else:
                        raise Exception("not enough money")
                # 平多
                elif order_type == ORDER_TYPE_CLOSE_LONG:
                    if current_position.availableLong >= order_amount:
                        self.account.cash += (order_amount * current_price)
                        current_position.availableLong -= order_amount
                        current_position.longAmount -= order_amount
                    else:
                        raise Exception("not enough position")
                # 平空
                elif order_type == ORDER_TYPE_CLOSE_SHORT:
                    if current_position.availableShort >= order_amount:
                        self.account.cash += (order_amount * current_price)
                        current_position.availableShort -= order_amount
                        current_position.shortAmount -= order_amount
                    else:
                        raise Exception("not enough position")

            # 按仓位比例交易
            elif 0 < order_pct <= 1:
                # 开多
                if order_type == ORDER_TYPE_LONG:
                    cost = current_price * (1 + self.slippage + self.buy_cost)
                    want_buy = self.account.cash * order_pct
                    if want_buy >= cost:
                        # 买的数量
                        order_amount = want_buy // cost
                        # 使用的现金
                        self.account.cash -= (want_buy - want_buy % cost)
                        self.update_position(current_position, order_amount, current_price, order_type)
                    else:
                        raise Exception("not enough money")
                # 开空
                elif order_type == ORDER_TYPE_SHORT:
                    need_money = (order_amount * current_price) * (1 + self.slippage + self.buy_cost)
                    if self.account.cash >= need_money:
                        self.account.cash -= need_money
                        self.update_position(current_position, order_amount, current_price, order_type)
                    else:
                        raise Exception("not enough money")
                # 平多
                elif order_type == ORDER_TYPE_CLOSE_LONG:
                    if current_position.availableLong > 1:
                        order_amount = math.floor(current_position.availableLong * order_pct)
                        if order_amount != 0:
                            self.account.cash += (order_amount * current_price)
                            current_position.availableLong -= order_amount
                            current_position.longAmount -= order_amount
                        else:
                            self.logger.warning("{} availableLong:{} order_pct:{} order_amount:{}", security_id,
                                                current_position.availableLong, order_pct, order_amount)
                    else:
                        raise Exception("not enough position")
                # 平空
                elif order_type == ORDER_TYPE_CLOSE_SHORT:
                    if current_position.availableShort > 1:
                        order_amount = math.floor(current_position.availableShort * order_pct)
                        if order_amount != 0:
                            self.account.cash += (order_amount * current_price)
                            current_position.availableLong -= order_amount
                            current_position.longAmount -= order_amount
                        else:
                            self.logger.warning("{} availableLong:{} order_pct:{} order_amount:{}", security_id,
                                                current_position.availableLong, order_pct, order_amount)
                    else:
                        raise Exception("not enough position")

            self.update_account(security_id, current_position, current_timestamp)