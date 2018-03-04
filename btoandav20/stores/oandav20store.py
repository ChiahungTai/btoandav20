#!/usr/bin/env python
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import collections
import json
import threading
import time as _time
from datetime import datetime, timedelta

import v20

import backtrader as bt
from backtrader.metabase import MetaParams
from backtrader.utils.py3 import queue, with_metaclass
from v20.account import Account


class MetaSingleton(MetaParams):
    '''Metaclass to make a metaclassed class a singleton'''
    def __init__(cls, name, bases, dct):
        super(MetaSingleton, cls).__init__(name, bases, dct)
        cls._singleton = None

    def __call__(cls, *args, **kwargs):
        if cls._singleton is None:
            cls._singleton = (
                super(MetaSingleton, cls).__call__(*args, **kwargs))

        return cls._singleton


class OandaV20Store(with_metaclass(MetaSingleton, object)):
    '''Singleton class wrapping to control the connections to Oanda v20.

    Params:

      - ``token`` (default:``None``): API access token

      - ``account`` (default: ``None``): account id

      - ``practice`` (default: ``False``): use the test environment

      - ``account_tmout`` (default: ``10.0``): refresh period for account
        value/cash refresh
    '''

    BrokerCls = None  # broker class will auto register
    DataCls = None  # data class will auto register

    # Oanda supported granularities
    _GRANULARITIES = {
        (bt.TimeFrame.Seconds, 5): 'S5',
        (bt.TimeFrame.Seconds, 10): 'S10',
        (bt.TimeFrame.Seconds, 15): 'S15',
        (bt.TimeFrame.Seconds, 30): 'S30',
        (bt.TimeFrame.Minutes, 1): 'M1',
        (bt.TimeFrame.Minutes, 2): 'M3',
        (bt.TimeFrame.Minutes, 3): 'M3',
        (bt.TimeFrame.Minutes, 4): 'M4',
        (bt.TimeFrame.Minutes, 5): 'M5',
        (bt.TimeFrame.Minutes, 10): 'M10',
        (bt.TimeFrame.Minutes, 15): 'M15',
        (bt.TimeFrame.Minutes, 30): 'M30',
        (bt.TimeFrame.Minutes, 60): 'H1',
        (bt.TimeFrame.Minutes, 120): 'H2',
        (bt.TimeFrame.Minutes, 180): 'H3',
        (bt.TimeFrame.Minutes, 240): 'H4',
        (bt.TimeFrame.Minutes, 360): 'H6',
        (bt.TimeFrame.Minutes, 480): 'H8',
        (bt.TimeFrame.Days, 1): 'D',
        (bt.TimeFrame.Weeks, 1): 'W',
        (bt.TimeFrame.Months, 1): 'M',
    }

    # Oanda api endpoints
    _OAPI_URL = ["api-fxtrade.oanda.com",
                 "api-fxpractice.oanda.com"]
    _OAPI_STREAM_URL = ["stream-fxtrade.oanda.com",
                        "stream-fxpractice.oanda.com"]

    params = (
        ('token', ''),
        ('account', ''),
        ('practice', False),
        ('account_tmout', 10.0),  # account balance refresh timeout
    )

    @classmethod
    def getdata(cls, *args, **kwargs):
        '''Returns ``DataCls`` with args, kwargs'''
        return cls.DataCls(*args, **kwargs)

    @classmethod
    def getbroker(cls, *args, **kwargs):
        '''Returns broker with *args, **kwargs from registered ``BrokerCls``'''
        return cls.BrokerCls(*args, **kwargs)

    def __init__(self):
        super(OandaV20Store, self).__init__()

        self.notifs = collections.deque()  # store notifications for cerebro

        self._cash = 0.0
        self._value = 0.0

        self._env = None  # reference to cerebro for general notifications
        self._evt_acct = threading.Event()

        self.broker = None  # broker instance
        self.datas = list()  # datas that have registered over start

        # init oanda v20 api context
        self.oapi = v20.Context(
            self._OAPI_URL[int(self.p.practice)],
            port=443,
            ssl=True,
            token=self.p.token,
            datetime_format="UNIX",
        )

        # init oanda v20 api stream context
        self.oapi_stream = v20.Context(
            self._OAPI_STREAM_URL[int(self.p.practice)],
            port=443,
            ssl=True,
            token=self.p.token,
            datetime_format="UNIX",
        )

    def start(self, data=None, broker=None):
        # Datas require some processing to kickstart data reception
        if data is None and broker is None:
            self.cash = None
            return

        if data is not None:
            self._env = data._env
            # For datas simulate a queue with None to kickstart co
            self.datas.append(data)

            if self.broker is not None:
                self.broker.data_started(data)

        elif broker is not None:
            self.broker = broker
            self.streaming_events()
            self.broker_threads()

    def stop(self):
        raise Exception("Not implemented")

    def put_notification(self, msg, *args, **kwargs):
        self.notifs.append((msg, args, kwargs))

    def get_notifications(self):
        '''Return the pending "store" notifications'''
        self.notifs.append(None)  # put a mark / threads could still append
        return [x for x in iter(self.notifs.popleft, None)]

    def get_positions(self):
        raise Exception("Not implemented")

    def get_granularity(self, timeframe, compression):
        return self._GRANULARITIES.get((timeframe, compression), None)

    def get_instrument(self, dataname):
        raise Exception("Not implemented")

    def candles(self, dataname, dtbegin, dtend, timeframe, compression,
                candleFormat, includeFirst):
        raise Exception("Not implemented")

    def get_cash(self):
        return self._cash

    def get_value(self):
        return self._value

    def streaming_events(self, tmout=None):
        q = queue.Queue()
        kwargs = {'q': q, 'tmout': tmout}

        t = threading.Thread(target=self._t_streaming_listener, kwargs=kwargs)
        t.daemon = True
        t.start()

        t = threading.Thread(target=self._t_streaming_events, kwargs=kwargs)
        t.daemon = True
        t.start()
        return q

    def streaming_prices(self, dataname, tmout=None):
        raise Exception("Not implemented")

    def broker_threads(self):
        self.q_account = queue.Queue()
        self.q_account.put(True)  # force an immediate update
        t = threading.Thread(target=self._t_account)
        t.daemon = True
        t.start()

        '''self.q_ordercreate = queue.Queue()
        t = threading.Thread(target=self._t_order_create)
        t.daemon = True
        t.start()

        self.q_orderclose = queue.Queue()
        t = threading.Thread(target=self._t_order_cancel)
        t.daemon = True
        t.start()'''

        # Wait once for the values to be set
        self._evt_acct.wait(self.p.account_tmout)

    def order_create(self, order, stopside=None, takeside=None, **kwargs):
        raise Exception("Not implemented")

    def order_cancel(self, order):
        raise Exception("Not implemented")

    def _t_streaming_listener(self, q, tmout=None):
        while True:
            trans = q.get()
            self._transaction(trans)

    def _t_streaming_events(self, q, tmout=None):
        if tmout is not None:
            _time.sleep(tmout)
        '''
                streamer = Streamer(q,
                                    environment=self._oenv,
                                    access_token=self.p.token,
                                    headers={'X-Accept-Datetime-Format': 'UNIX'})

                streamer.events(ignore_heartbeat=False)
        '''
        # TODO

    def _transaction(self, trans):
        # Invoked from Streaming Events. May actually receive an event for an
        # oid which has not yet been returned after creating an order. Hence
        # store if not yet seen, else forward to processor
        print(trans)
        # TODO

    def _t_account(self):
        # Invoked from api thread, fetches account summary and sets current
        # values from oanda account
        while True:
            try:
                msg = self.q_account.get(timeout=self.p.account_tmout)
                if msg is None:
                    break  # end of thread
            except queue.Empty:  # tmout -> time to refresh
                pass

            try:
                response = self.oapi.account.summary(self.p.account)
                accinfo = response.get('account')
            except Exception as e:
                self.put_notification(e)
                continue

            try:
                self._cash = accinfo.marginAvailable
                self._value = accinfo.balance
            except KeyError:
                pass

            self._evt_acct.set()


    def _t_order_create(self):
        '''while True:
            msg = self.q_ordercreate.get()
            if msg is None:
                break

            oref, okwargs = msg
            try:
                o = self.oapi.create_order(self.p.account, **okwargs)
            except Exception as e:
                self.put_notification(e)
                self.broker._reject(order.ref)
                return

            # Ids are delivered in different fields and all must be fetched to
            # match them (as executions) to the order generated here
            _o = {'id': None}
            oids = list()
            for oidfield in self._OIDSINGLE:
                if oidfield in o and 'id' in o[oidfield]:
                    oids.append(o[oidfield]['id'])

            for oidfield in self._OIDMULTIPLE:
                if oidfield in o:
                    for suboidfield in o[oidfield]:
                        oids.append(suboidfield['id'])

            if not oids:
                self.broker._reject(oref)
                return

            self._orders[oref] = oids[0]
            self.broker._submit(oref)
            if okwargs['type'] == 'market':
                self.broker._accept(oref)  # taken immediately

            for oid in oids:
                self._ordersrev[oid] = oref  # maps ids to backtrader order

                # An transaction may have happened and was stored
                tpending = self._transpend[oid]
                tpending.append(None)  # eom marker
                while True:
                    trans = tpending.popleft()
                    if trans is None:
                        break
                    self._process_transaction(oid, trans)
        '''

    def _t_order_cancel(self):
        '''while True:
            oref = self.q_orderclose.get()
            if oref is None:
                break

            oid = self._orders.get(oref, None)
            if oid is None:
                continue  # the order is no longer there
            try:
                o = self.oapi.close_order(self.p.account, oid)
            except Exception as e:
                continue  # not cancelled - FIXME: notify

            self.broker._cancel(oref)
        '''