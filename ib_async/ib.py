"""High-level interface to Interactive Brokers."""

import asyncio
import copy
import datetime
import logging
import time
from enum import auto, Flag
from typing import Any, Awaitable, Iterator, List, Optional, Union

from eventkit import Event

import ib_async.util as util
from ib_async.client import Client
from ib_async.contract import Contract, ContractDescription, ContractDetails
from ib_async.objects import (
    AccountValue,
    BarDataList,
    DepthMktDataDescription,
    Execution,
    ExecutionFilter,
    Fill,
    HistogramData,
    HistoricalNews,
    HistoricalSchedule,
    IBDefaults,
    NewsArticle,
    NewsBulletin,
    NewsProvider,
    NewsTick,
    OptionChain,
    OptionComputation,
    PnL,
    PnLSingle,
    PortfolioItem,
    Position,
    PriceIncrement,
    RealTimeBarList,
    ScanDataList,
    ScannerSubscription,
    SmartComponent,
    TagValue,
    TradeLogEntry,
    WshEventData,
)
from ib_async.order import (
    BracketOrder,
    LimitOrder,
    Order,
    OrderState,
    OrderStateNumeric,
    OrderStatus,
    StopOrder,
    Trade,
)
from ib_async.ticker import Ticker
from ib_async.wrapper import Wrapper


class StartupFetch(Flag):
    POSITIONS = auto()
    ORDERS_OPEN = auto()
    ORDERS_COMPLETE = auto()
    ACCOUNT_UPDATES = auto()
    SUB_ACCOUNT_UPDATES = auto()
    EXECUTIONS = auto()


StartupFetchNONE = StartupFetch(0)

StartupFetchALL = (
    StartupFetch.POSITIONS
    | StartupFetch.ORDERS_OPEN
    | StartupFetch.ORDERS_COMPLETE
    | StartupFetch.ACCOUNT_UPDATES
    | StartupFetch.SUB_ACCOUNT_UPDATES
    | StartupFetch.EXECUTIONS
)


class IB:
    """
    Provides both a blocking and an asynchronous interface
    to the IB API, using asyncio networking and event loop.

    The IB class offers direct access to the current state, such as
    orders, executions, positions, tickers etc. This state is
    automatically kept in sync with the TWS/IBG application.

    This class has most request methods of EClient, with the
    same names and parameters (except for the reqId parameter
    which is not needed anymore).
    Request methods that return a result come in two versions:

    * Blocking: Will block until complete and return the result.
      The current state will be kept updated while the request is ongoing;

    * Asynchronous: All methods that have the "Async" postfix.
      Implemented as coroutines or methods that return a Future and
      intended for advanced users.

    **The One Rule:**

    While some of the request methods are blocking from the perspective
    of the user, the framework will still keep spinning in the background
    and handle all messages received from TWS/IBG. It is important to
    not block the framework from doing its work. If, for example,
    the user code spends much time in a calculation, or uses time.sleep()
    with a long delay, the framework will stop spinning, messages
    accumulate and things may go awry.

    The one rule when working with the IB class is therefore that

    **user code may not block for too long**.

    To be clear, the IB request methods are okay to use and do not
    count towards the user operation time, no matter how long the
    request takes to finish.

    So what is "too long"? That depends on the situation. If, for example,
    the timestamp of tick data is to remain accurate within a millisecond,
    then the user code must not spend longer than a millisecond. If, on
    the other extreme, there is very little incoming data and there
    is no desire for accurate timestamps, then the user code can block
    for hours.

    If a user operation takes a long time then it can be farmed out
    to a different process.
    Alternatively the operation can be made such that it periodically
    calls IB.sleep(0); This will let the framework handle any pending
    work and return when finished. The operation should be aware
    that the current state may have been updated during the sleep(0) call.

    For introducing a delay, never use time.sleep() but use
    :meth:`.sleep` instead.

    Parameters:
        RequestTimeout (float): Timeout (in seconds) to wait for a
          blocking request to finish before raising ``asyncio.TimeoutError``.
          The default value of 0 will wait indefinitely.
          Note: This timeout is not used for the ``*Async`` methods.
        RaiseRequestErrors (bool):
          Specifies the behaviour when certain API requests fail:

          * :data:`False`: Silently return an empty result;
          * :data:`True`: Raise a :class:`.RequestError`.
        MaxSyncedSubAccounts (int): Do not use sub-account updates
          if the number of sub-accounts exceeds this number (50 by default).
        TimezoneTWS (str): Specifies what timezone TWS (or gateway)
          is using. The default is to assume local system timezone.

    Events:
        * ``connectedEvent`` ():
          Is emitted after connecting and synchronzing with TWS/gateway.

        * ``disconnectedEvent`` ():
          Is emitted after disconnecting from TWS/gateway.

        * ``updateEvent`` ():
          Is emitted after a network packet has been handled.

        * ``pendingTickersEvent`` (tickers: Set[:class:`.Ticker`]):
          Emits the set of tickers that have been updated during the last
          update and for which there are new ticks, tickByTicks or domTicks.

        * ``barUpdateEvent`` (bars: :class:`.BarDataList`,
          hasNewBar: bool): Emits the bar list that has been updated in
          real time. If a new bar has been added then hasNewBar is True,
          when the last bar has changed it is False.

        * ``newOrderEvent`` (trade: :class:`.Trade`):
          Emits a newly placed trade.

        * ``orderModifyEvent`` (trade: :class:`.Trade`):
          Emits when order is modified.

        * ``cancelOrderEvent`` (trade: :class:`.Trade`):
          Emits a trade directly after requesting for it to be cancelled.

        * ``openOrderEvent`` (trade: :class:`.Trade`):
          Emits the trade with open order.

        * ``orderStatusEvent`` (trade: :class:`.Trade`):
          Emits the changed order status of the ongoing trade.

        * ``execDetailsEvent`` (trade: :class:`.Trade`, fill: :class:`.Fill`):
          Emits the fill together with the ongoing trade it belongs to.

        * ``commissionReportEvent`` (trade: :class:`.Trade`,
          fill: :class:`.Fill`, report: :class:`.CommissionReport`):
          The commission report is emitted after the fill that it belongs to.

        * ``updatePortfolioEvent`` (item: :class:`.PortfolioItem`):
          A portfolio item has changed.

        * ``positionEvent`` (position: :class:`.Position`):
          A position has changed.

        * ``accountValueEvent`` (value: :class:`.AccountValue`):
          An account value has changed.

        * ``accountSummaryEvent`` (value: :class:`.AccountValue`):
          An account value has changed.

        * ``pnlEvent`` (entry: :class:`.PnL`):
          A profit- and loss entry is updated.

        * ``pnlSingleEvent`` (entry: :class:`.PnLSingle`):
          A profit- and loss entry for a single position is updated.

        * ``tickNewsEvent`` (news: :class:`.NewsTick`):
          Emit a new news headline.

        * ``newsBulletinEvent`` (bulletin: :class:`.NewsBulletin`):
          Emit a new news bulletin.

        * ``scannerDataEvent`` (data: :class:`.ScanDataList`):
          Emit data from a scanner subscription.

        * ``wshMetaEvent`` (dataJson: str):
          Emit WSH metadata.

        * ``wshEvent`` (dataJson: str):
          Emit WSH event data (such as earnings dates, dividend dates,
          options expiration dates, splits, spinoffs and conferences).

        * ``errorEvent`` (reqId: int, errorCode: int, errorString: str,
          contract: :class:`.Contract`):
          Emits the reqId/orderId and TWS error code and string (see
          https://interactivebrokers.github.io/tws-api/message_codes.html)
          together with the contract the error applies to (or None if no
          contract applies).

        * ``timeoutEvent`` (idlePeriod: float):
          Is emitted if no data is received for longer than the timeout period
          specified with :meth:`.setTimeout`. The value emitted is the period
          in seconds since the last update.

        Note that it is not advisable to place new requests inside an event
        handler as it may lead to too much recursion.
    """

    events = (
        "connectedEvent",
        "disconnectedEvent",
        "updateEvent",
        "pendingTickersEvent",
        "barUpdateEvent",
        "newOrderEvent",
        "orderModifyEvent",
        "cancelOrderEvent",
        "openOrderEvent",
        "orderStatusEvent",
        "execDetailsEvent",
        "commissionReportEvent",
        "updatePortfolioEvent",
        "positionEvent",
        "accountValueEvent",
        "accountSummaryEvent",
        "pnlEvent",
        "pnlSingleEvent",
        "scannerDataEvent",
        "tickNewsEvent",
        "newsBulletinEvent",
        "wshMetaEvent",
        "wshEvent",
        "errorEvent",
        "timeoutEvent",
    )

    RequestTimeout: float = 0
    RaiseRequestErrors: bool = False
    MaxSyncedSubAccounts: int = 50
    TimezoneTWS: str = ""

    def __init__(self, defaults: IBDefaults = IBDefaults()):
        self._createEvents()
        self.wrapper = Wrapper(self, defaults=defaults)
        self.client = Client(self.wrapper)
        self.errorEvent += self._onError
        self.client.apiEnd += self.disconnectedEvent
        self._logger = logging.getLogger("ib_async.ib")

    def _createEvents(self):
        self.connectedEvent = Event("connectedEvent")
        self.disconnectedEvent = Event("disconnectedEvent")
        self.updateEvent = Event("updateEvent")
        self.pendingTickersEvent = Event("pendingTickersEvent")
        self.barUpdateEvent = Event("barUpdateEvent")
        self.newOrderEvent = Event("newOrderEvent")
        self.orderModifyEvent = Event("orderModifyEvent")
        self.cancelOrderEvent = Event("cancelOrderEvent")
        self.openOrderEvent = Event("openOrderEvent")
        self.orderStatusEvent = Event("orderStatusEvent")
        self.execDetailsEvent = Event("execDetailsEvent")
        self.commissionReportEvent = Event("commissionReportEvent")
        self.updatePortfolioEvent = Event("updatePortfolioEvent")
        self.positionEvent = Event("positionEvent")
        self.accountValueEvent = Event("accountValueEvent")
        self.accountSummaryEvent = Event("accountSummaryEvent")
        self.pnlEvent = Event("pnlEvent")
        self.pnlSingleEvent = Event("pnlSingleEvent")
        self.scannerDataEvent = Event("scannerDataEvent")
        self.tickNewsEvent = Event("tickNewsEvent")
        self.newsBulletinEvent = Event("newsBulletinEvent")
        self.wshMetaEvent = Event("wshMetaEvent")
        self.wshEvent = Event("wshEvent")
        self.errorEvent = Event("errorEvent")
        self.timeoutEvent = Event("timeoutEvent")

    def __del__(self):
        self.disconnect()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.disconnect()

    def __repr__(self):
        conn = (
            f"connected to {self.client.host}:"
            f"{self.client.port} clientId={self.client.clientId}"
            if self.client.isConnected()
            else "not connected"
        )
        return f"<{self.__class__.__qualname__} {conn}>"

    def connect(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        clientId: int = 1,
        timeout: float = 4,
        readonly: bool = False,
        account: str = "",
        raiseSyncErrors: bool = False,
        fetchFields: StartupFetch = StartupFetchALL,
    ):
        """
        Connect to a running TWS or IB gateway application.
        After the connection is made the client is fully synchronized
        and ready to serve requests.

        This method is blocking.

        Args:
            host: Host name or IP address.
            port: Port number.
            clientId: ID number to use for this client; must be unique per
              connection. Setting clientId=0 will automatically merge manual
              TWS trading with this client.
            timeout: If establishing the connection takes longer than
              ``timeout`` seconds then the ``asyncio.TimeoutError`` exception
              is raised. Set to 0 to disable timeout.
            readonly: Set to ``True`` when API is in read-only mode.
            account: Main account to receive updates for.
            raiseSyncErrors: When ``True`` this will cause an initial
              sync request error to raise a `ConnectionError``.
              When ``False`` the error will only be logged at error level.
           fetchFields: By default, all account data is loaded and cached
              when a new connection is made. You can optionally disable all
              or some of the account attribute fetching during a connection
              using the StartupFetch field flags. See ``StartupFetch`` in ``ib.py``
              for member details. There is also StartupFetchNONE and StartupFetchALL
              as shorthand. Individual flag field members can be added or removed
              to the ``fetchFields`` parameter as needed.
        """
        return self._run(
            self.connectAsync(
                host,
                port,
                clientId,
                timeout,
                readonly,
                account,
                raiseSyncErrors,
                fetchFields,
            )
        )

    def disconnect(self) -> str | None:
        """
        Disconnect from a TWS or IB gateway application.
        This will clear all session state.
        """
        if not self.client.isConnected():
            return None

        stats = self.client.connectionStats()

        status = (
            f"Disconnecting from {self.client.host}:{self.client.port}, "
            f"{util.formatSI(stats.numBytesSent)}B sent "
            f"in {stats.numMsgSent} messages, "
            f"{util.formatSI(stats.numBytesRecv)}B received "
            f"in {stats.numMsgRecv} messages, "
            f"session time {util.formatSI(stats.duration)}s."
        )

        self._logger.info(status)
        self.client.disconnect()
        self.disconnectedEvent.emit()

        # clear ALL internal state from this connection
        self.wrapper.reset()

        return status

    def isConnected(self) -> bool:
        """Is there an API connection to TWS or IB gateway?"""
        return self.client.isReady()

    def _onError(self, reqId, errorCode, errorString, contract):
        if errorCode == 1102:
            # "Connectivity between IB and Trader Workstation has been
            # restored": Resubscribe to account summary.
            asyncio.ensure_future(self.reqAccountSummaryAsync())

    run = staticmethod(util.run)
    schedule = staticmethod(util.schedule)
    sleep = staticmethod(util.sleep)
    timeRange = staticmethod(util.timeRange)
    timeRangeAsync = staticmethod(util.timeRangeAsync)
    waitUntil = staticmethod(util.waitUntil)

    def _run(self, *awaitables: Awaitable):
        return util.run(*awaitables, timeout=self.RequestTimeout)

    def waitOnUpdate(self, timeout: float = 0) -> bool:
        """
        Wait on any new update to arrive from the network.

        Args:
            timeout: Maximum time in seconds to wait.
                If 0 then no timeout is used.

        .. note::
            A loop with ``waitOnUpdate`` should not be used to harvest
            tick data from tickers, since some ticks can go missing.
            This happens when multiple updates occur almost simultaneously;
            The ticks from the first update are then cleared.
            Use events instead to prevent this.

        Returns:
            ``True`` if not timed-out, ``False`` otherwise.
        """
        if timeout:
            try:
                util.run(asyncio.wait_for(self.updateEvent, timeout))
            except asyncio.TimeoutError:
                return False
        else:
            util.run(self.updateEvent)

        return True

    def loopUntil(self, condition=None, timeout: float = 0) -> Iterator[object]:
        """
        Iterate until condition is met, with optional timeout in seconds.
        The yielded value is that of the condition or False when timed out.

        Args:
            condition: Predicate function that is tested after every network
            update.
            timeout: Maximum time in seconds to wait.
                If 0 then no timeout is used.
        """
        endTime = time.time() + timeout
        while True:
            test = condition and condition()
            if test:
                yield test
                return

            if timeout and time.time() > endTime:
                yield False
                return

            yield test

            self.waitOnUpdate(endTime - time.time() if timeout else 0)

    def setTimeout(self, timeout: float = 60):
        """
        Set a timeout for receiving messages from TWS/IBG, emitting
        ``timeoutEvent`` if there is no incoming data for too long.

        The timeout fires once per connected session but can be set again
        after firing or after a reconnect.

        Args:
            timeout: Timeout in seconds.
        """
        self.wrapper.setTimeout(timeout)

    def managedAccounts(self) -> list[str]:
        """List of account names."""
        return list(self.wrapper.accounts)

    def accountValues(self, account: str = "") -> list[AccountValue]:
        """
        List of account values for the given account,
        or of all accounts if account is left blank.

        Args:
            account: If specified, filter for this account name.
        """
        if account:
            return [
                v for v in self.wrapper.accountValues.values() if v.account == account
            ]

        return list(self.wrapper.accountValues.values())

    def accountSummary(self, account: str = "") -> list[AccountValue]:
        """
        List of account values for the given account,
        or of all accounts if account is left blank.

        This method is blocking on first run, non-blocking after that.

        Args:
            account: If specified, filter for this account name.
        """
        return self._run(self.accountSummaryAsync(account))

    def portfolio(self, account: str = "") -> list[PortfolioItem]:
        """
        List of portfolio items for the given account,
        or of all retrieved portfolio items if account is left blank.

        Args:
            account: If specified, filter for this account name.
        """
        if account:
            return list(self.wrapper.portfolio[account].values())

        return [v for d in self.wrapper.portfolio.values() for v in d.values()]

    def positions(self, account: str = "") -> list[Position]:
        """
        List of positions for the given account,
        or of all accounts if account is left blank.

        Args:
            account: If specified, filter for this account name.
        """
        if account:
            return list(self.wrapper.positions[account].values())

        return [v for d in self.wrapper.positions.values() for v in d.values()]

    def pnl(self, account="", modelCode="") -> list[PnL]:
        """
        List of subscribed :class:`.PnL` objects (profit and loss),
        optionally filtered by account and/or modelCode.

        The :class:`.PnL` objects are kept live updated.

        Args:
            account: If specified, filter for this account name.
            modelCode: If specified, filter for this account model.
        """
        return [
            v
            for v in self.wrapper.reqId2PnL.values()
            if (not account or v.account == account)
            and (not modelCode or v.modelCode == modelCode)
        ]

    def pnlSingle(
        self, account: str = "", modelCode: str = "", conId: int = 0
    ) -> list[PnLSingle]:
        """
        List of subscribed :class:`.PnLSingle` objects (profit and loss for
        single positions).

        The :class:`.PnLSingle` objects are kept live updated.

        Args:
            account: If specified, filter for this account name.
            modelCode: If specified, filter for this account model.
            conId: If specified, filter for this contract ID.
        """
        return [
            v
            for v in self.wrapper.reqId2PnlSingle.values()
            if (not account or v.account == account)
            and (not modelCode or v.modelCode == modelCode)
            and (not conId or v.conId == conId)
        ]

    def trades(self) -> list[Trade]:
        """List of all order trades from this session."""
        return list(self.wrapper.trades.values())

    def openTrades(self) -> list[Trade]:
        """List of all open order trades."""
        return [
            v
            for v in self.wrapper.trades.values()
            if v.orderStatus.status not in OrderStatus.DoneStates
        ]

    def orders(self) -> list[Order]:
        """List of all orders from this session."""
        return list(trade.order for trade in self.wrapper.trades.values())

    def openOrders(self) -> list[Order]:
        """List of all open orders."""
        return [
            trade.order
            for trade in self.wrapper.trades.values()
            if trade.orderStatus.status not in OrderStatus.DoneStates
        ]

    def fills(self) -> list[Fill]:
        """List of all fills from this session."""
        return list(self.wrapper.fills.values())

    def executions(self) -> list[Execution]:
        """List of all executions from this session."""
        return list(fill.execution for fill in self.wrapper.fills.values())

    def ticker(self, contract: Contract) -> Optional[Ticker]:
        """
        Get ticker of the given contract. It must have been requested before
        with reqMktData with the same contract object. The ticker may not be
        ready yet if called directly after :meth:`.reqMktData`.

        Args:
            contract: Contract to get ticker for.
        """
        return self.wrapper.tickers.get(hash(contract))

    def tickers(self) -> list[Ticker]:
        """Get a list of all tickers."""
        return list(self.wrapper.tickers.values())

    def pendingTickers(self) -> list[Ticker]:
        """Get a list of all tickers that have pending ticks or domTicks."""
        return list(self.wrapper.pendingTickers)

    def realtimeBars(self) -> list[Union[BarDataList, RealTimeBarList]]:
        """
        Get a list of all live updated bars. These can be 5 second realtime
        bars or live updated historical bars.
        """
        return list(self.wrapper.reqId2Subscriber.values())

    def newsTicks(self) -> list[NewsTick]:
        """
        List of ticks with headline news.
        The article itself can be retrieved with :meth:`.reqNewsArticle`.
        """
        return self.wrapper.newsTicks

    def newsBulletins(self) -> list[NewsBulletin]:
        """List of IB news bulletins."""
        return list(self.wrapper.msgId2NewsBulletin.values())

    def reqTickers(
        self, *contracts: Contract, regulatorySnapshot: bool = False
    ) -> list[Ticker]:
        """
        Request and return a list of snapshot tickers.
        The list is returned when all tickers are ready.

        This method is blocking.

        Args:
            contracts: Contracts to get tickers for.
            regulatorySnapshot: Request NBBO snapshots (may incur a fee).
        """
        return self._run(
            self.reqTickersAsync(*contracts, regulatorySnapshot=regulatorySnapshot)
        )

    def qualifyContracts(self, *contracts: Contract) -> list[Contract]:
        """
        Fully qualify the given contracts in-place. This will fill in
        the missing fields in the contract, especially the conId.

        Returns a list of contracts that have been successfully qualified.

        This method is blocking.

        Args:
            contracts: Contracts to qualify.
        """
        return self._run(self.qualifyContractsAsync(*contracts))

    def bracketOrder(
        self,
        action: str,
        quantity: float,
        limitPrice: float,
        takeProfitPrice: float,
        stopLossPrice: float,
        **kwargs,
    ) -> BracketOrder:
        """
        Create a limit order that is bracketed by a take-profit order and
        a stop-loss order. Submit the bracket like:

        .. code-block:: python

            for o in bracket:
                ib.placeOrder(contract, o)

        https://interactivebrokers.github.io/tws-api/bracket_order.html

        Args:
            action: 'BUY' or 'SELL'.
            quantity: Size of order.
            limitPrice: Limit price of entry order.
            takeProfitPrice: Limit price of profit order.
            stopLossPrice: Stop price of loss order.
        """
        assert action in ("BUY", "SELL")
        reverseAction = "BUY" if action == "SELL" else "SELL"
        parent = LimitOrder(
            action,
            quantity,
            limitPrice,
            orderId=self.client.getReqId(),
            transmit=False,
            **kwargs,
        )
        takeProfit = LimitOrder(
            reverseAction,
            quantity,
            takeProfitPrice,
            orderId=self.client.getReqId(),
            transmit=False,
            parentId=parent.orderId,
            **kwargs,
        )
        stopLoss = StopOrder(
            reverseAction,
            quantity,
            stopLossPrice,
            orderId=self.client.getReqId(),
            transmit=True,
            parentId=parent.orderId,
            **kwargs,
        )

        return BracketOrder(parent, takeProfit, stopLoss)

    @staticmethod
    def oneCancelsAll(orders: list[Order], ocaGroup: str, ocaType: int) -> list[Order]:
        """
        Place the trades in the same One Cancels All (OCA) group.

        https://interactivebrokers.github.io/tws-api/oca.html

        Args:
            orders: The orders that are to be placed together.
        """
        for o in orders:
            o.ocaGroup = ocaGroup
            o.ocaType = ocaType
        return orders

    def whatIfOrder(self, contract: Contract, order: Order) -> OrderState:
        """
        Retrieve commission and margin impact without actually
        placing the order. The given order will not be modified in any way.

        This method is blocking.

        Args:
            contract: Contract to test.
            order: Order to test.
        """
        return self._run(self.whatIfOrderAsync(contract, order))

    def placeOrder(self, contract: Contract, order: Order) -> Trade:
        """
        Place a new order or modify an existing order.
        Returns a Trade that is kept live updated with
        status changes, fills, etc.

        Args:
            contract: Contract to use for order.
            order: The order to be placed.
        """
        orderId = order.orderId or self.client.getReqId()
        self.client.placeOrder(orderId, contract, order)
        now = datetime.datetime.now(self.wrapper.defaultTimezone)
        key = self.wrapper.orderKey(self.wrapper.clientId, orderId, order.permId)
        trade = self.wrapper.trades.get(key)
        if trade:
            # this is a modification of an existing order
            assert trade.orderStatus.status not in OrderStatus.DoneStates
            logEntry = TradeLogEntry(now, trade.orderStatus.status, "Modify")
            trade.log.append(logEntry)
            self._logger.info(f"placeOrder: Modify order {trade}")
            trade.modifyEvent.emit(trade)
            self.orderModifyEvent.emit(trade)
        else:
            # this is a new order
            order.clientId = self.wrapper.clientId
            order.orderId = orderId
            orderStatus = OrderStatus(orderId=orderId, status=OrderStatus.PendingSubmit)
            logEntry = TradeLogEntry(now, orderStatus.status)
            trade = Trade(contract, order, orderStatus, [], [logEntry])
            self.wrapper.trades[key] = trade
            self._logger.info(f"placeOrder: New order {trade}")
            self.newOrderEvent.emit(trade)

        return trade

    def cancelOrder(
        self, order: Order, manualCancelOrderTime: str = ""
    ) -> Optional[Trade]:
        """
        Cancel the order and return the Trade it belongs to.

        Args:
            order: The order to be canceled.
            manualCancelOrderTime: For audit trail.
        """
        self.client.cancelOrder(order.orderId, manualCancelOrderTime)
        now = datetime.datetime.now(self.wrapper.defaultTimezone)
        key = self.wrapper.orderKey(order.clientId, order.orderId, order.permId)
        trade = self.wrapper.trades.get(key)
        if trade:
            if not trade.isDone():
                status = trade.orderStatus.status
                if (
                    status == OrderStatus.PendingSubmit
                    and not order.transmit
                    or status == OrderStatus.Inactive
                ):
                    newStatus = OrderStatus.Cancelled
                else:
                    newStatus = OrderStatus.PendingCancel

                logEntry = TradeLogEntry(now, newStatus)
                trade.log.append(logEntry)
                trade.orderStatus.status = newStatus
                self._logger.info(f"cancelOrder: {trade}")
                trade.cancelEvent.emit(trade)
                trade.statusEvent.emit(trade)
                self.cancelOrderEvent.emit(trade)
                self.orderStatusEvent.emit(trade)
                if newStatus == OrderStatus.Cancelled:
                    trade.cancelledEvent.emit(trade)
        else:
            self._logger.error(f"cancelOrder: Unknown orderId {order.orderId}")

        return trade

    def reqGlobalCancel(self):
        """
        Cancel all active trades including those placed by other
        clients or TWS/IB gateway.
        """
        self.client.reqGlobalCancel()
        self._logger.info("reqGlobalCancel")

    def reqCurrentTime(self) -> datetime.datetime:
        """
        Request TWS current time.

        This method is blocking.
        """
        return self._run(self.reqCurrentTimeAsync())

    def reqAccountUpdates(self, account: str = ""):
        """
        This is called at startup - no need to call again.

        Request account and portfolio values of the account
        and keep updated. Returns when both account values and portfolio
        are filled.

        This method is blocking.

        Args:
            account: If specified, filter for this account name.
        """
        self._run(self.reqAccountUpdatesAsync(account))

    def reqAccountUpdatesMulti(self, account: str = "", modelCode: str = ""):
        """
        It is recommended to use :meth:`.accountValues` instead.

        Request account values of multiple accounts and keep updated.

        This method is blocking.

        Args:
            account: If specified, filter for this account name.
            modelCode: If specified, filter for this account model.
        """
        self._run(self.reqAccountUpdatesMultiAsync(account, modelCode))

    def reqAccountSummary(self):
        """
        It is recommended to use :meth:`.accountSummary` instead.

        Request account values for all accounts and keep them updated.
        Returns when account summary is filled.

        This method is blocking.
        """
        self._run(self.reqAccountSummaryAsync())

    def reqAutoOpenOrders(self, autoBind: bool = True):
        """
        Bind manual TWS orders so that they can be managed from this client.
        The clientId must be 0 and the TWS API setting "Use negative numbers
        to bind automatic orders" must be checked.

        This request is automatically called when clientId=0.

        https://interactivebrokers.github.io/tws-api/open_orders.html
        https://interactivebrokers.github.io/tws-api/modifying_orders.html

        Args:
            autoBind: Set binding on or off.
        """
        self.client.reqAutoOpenOrders(autoBind)

    def reqOpenOrders(self) -> list[Trade]:
        """
        Request and return a list of open orders.

        This method can give stale information where a new open order is not
        reported or an already filled or cancelled order is reported as open.
        It is recommended to use the more reliable and much faster
        :meth:`.openTrades` or :meth:`.openOrders` methods instead.

        This method is blocking.
        """
        return self._run(self.reqOpenOrdersAsync())

    def reqAllOpenOrders(self) -> list[Trade]:
        """
        Request and return a list of all open orders over all clients.
        Note that the orders of other clients will not be kept in sync,
        use the master clientId mechanism instead to see other
        client's orders that are kept in sync.
        """
        return self._run(self.reqAllOpenOrdersAsync())

    def reqCompletedOrders(self, apiOnly: bool) -> list[Trade]:
        """
        Request and return a list of completed trades.

        Args:
            apiOnly: Request only API orders (not manually placed TWS orders).
        """
        return self._run(self.reqCompletedOrdersAsync(apiOnly))

    def reqExecutions(self, execFilter: Optional[ExecutionFilter] = None) -> list[Fill]:
        """
        It is recommended to use :meth:`.fills`  or
        :meth:`.executions` instead.

        Request and return a list of fills.

        This method is blocking.

        Args:
            execFilter: If specified, return executions that match the filter.
        """
        return self._run(self.reqExecutionsAsync(execFilter))

    def reqPositions(self) -> list[Position]:
        """
        It is recommended to use :meth:`.positions` instead.

        Request and return a list of positions for all accounts.

        This method is blocking.
        """
        return self._run(self.reqPositionsAsync())

    def reqPnL(self, account: str, modelCode: str = "") -> PnL:
        """
        Start a subscription for profit and loss events.

        Returns a :class:`.PnL` object that is kept live updated.
        The result can also be queried from :meth:`.pnl`.

        https://interactivebrokers.github.io/tws-api/pnl.html

        Args:
            account: Subscribe to this account.
            modelCode: If specified, filter for this account model.
        """
        key = (account, modelCode)
        assert key not in self.wrapper.pnlKey2ReqId

        reqId = self.client.getReqId()
        self.wrapper.pnlKey2ReqId[key] = reqId
        pnl = PnL(account, modelCode)
        self.wrapper.reqId2PnL[reqId] = pnl
        self.client.reqPnL(reqId, account, modelCode)

        return pnl

    def cancelPnL(self, account, modelCode: str = ""):
        """
        Cancel PnL subscription.

        Args:
            account: Cancel for this account.
            modelCode: If specified, cancel for this account model.
        """
        key = (account, modelCode)
        reqId = self.wrapper.pnlKey2ReqId.pop(key, None)
        if reqId:
            self.client.cancelPnL(reqId)
            self.wrapper.reqId2PnL.pop(reqId, None)
        else:
            self._logger.error(
                "cancelPnL: No subscription for "
                f"account {account}, modelCode {modelCode}"
            )

    def reqPnLSingle(self, account: str, modelCode: str, conId: int) -> PnLSingle:
        """
        Start a subscription for profit and loss events for single positions.

        Returns a :class:`.PnLSingle` object that is kept live updated.
        The result can also be queried from :meth:`.pnlSingle`.

        https://interactivebrokers.github.io/tws-api/pnl.html

        Args:
            account: Subscribe to this account.
            modelCode: Filter for this account model.
            conId: Filter for this contract ID.
        """
        key = (account, modelCode, conId)
        assert key not in self.wrapper.pnlSingleKey2ReqId

        reqId = self.client.getReqId()
        self.wrapper.pnlSingleKey2ReqId[key] = reqId
        pnlSingle = PnLSingle(account, modelCode, conId)
        self.wrapper.reqId2PnlSingle[reqId] = pnlSingle
        self.client.reqPnLSingle(reqId, account, modelCode, conId)

        return pnlSingle

    def cancelPnLSingle(self, account: str, modelCode: str, conId: int):
        """
        Cancel PnLSingle subscription for the given account, modelCode
        and conId.

        Args:
            account: Cancel for this account name.
            modelCode: Cancel for this account model.
            conId: Cancel for this contract ID.
        """
        key = (account, modelCode, conId)
        reqId = self.wrapper.pnlSingleKey2ReqId.pop(key, None)
        if reqId:
            self.client.cancelPnLSingle(reqId)
            self.wrapper.reqId2PnlSingle.pop(reqId, None)
        else:
            self._logger.error(
                "cancelPnLSingle: No subscription for "
                f"account {account}, modelCode {modelCode}, conId {conId}"
            )

    def reqContractDetails(self, contract: Contract) -> list[ContractDetails]:
        """
        Get a list of contract details that match the given contract.
        If the returned list is empty then the contract is not known;
        If the list has multiple values then the contract is ambiguous.

        The fully qualified contract is available in the the
        ContractDetails.contract attribute.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/contract_details.html

        Args:
            contract: The contract to get details for.
        """
        return self._run(self.reqContractDetailsAsync(contract))

    def reqMatchingSymbols(self, pattern: str) -> list[ContractDescription]:
        """
        Request contract descriptions of contracts that match a pattern.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/matching_symbols.html

        Args:
            pattern: The first few letters of the ticker symbol, or for
                longer strings a character sequence matching a word in
                the security name.
        """
        return self._run(self.reqMatchingSymbolsAsync(pattern))

    def reqMarketRule(self, marketRuleId: int) -> PriceIncrement:
        """
        Request price increments rule.

        https://interactivebrokers.github.io/tws-api/minimum_increment.html

        Args:
            marketRuleId: ID of market rule.
                The market rule IDs for a contract can be obtained
                via :meth:`.reqContractDetails` from
                :class:`.ContractDetails`.marketRuleIds,
                which contains a comma separated string of market rule IDs.
        """
        return self._run(self.reqMarketRuleAsync(marketRuleId))

    def reqRealTimeBars(
        self,
        contract: Contract,
        barSize: int,
        whatToShow: str,
        useRTH: bool,
        realTimeBarsOptions: list[TagValue] = [],
    ) -> RealTimeBarList:
        """
        Request realtime 5 second bars.

        https://interactivebrokers.github.io/tws-api/realtime_bars.html

        Args:
            contract: Contract of interest.
            barSize: Must be 5.
            whatToShow: Specifies the source for constructing bars.
                Can be 'TRADES', 'MIDPOINT', 'BID' or 'ASK'.
            useRTH: If True then only show data from within Regular
                Trading Hours, if False then show all data.
            realTimeBarsOptions: Unknown.
        """
        reqId = self.client.getReqId()
        bars = RealTimeBarList()
        bars.reqId = reqId
        bars.contract = contract
        bars.barSize = barSize
        bars.whatToShow = whatToShow
        bars.useRTH = useRTH
        bars.realTimeBarsOptions = realTimeBarsOptions or []
        self.wrapper.startSubscription(reqId, bars, contract)
        self.client.reqRealTimeBars(
            reqId, contract, barSize, whatToShow, useRTH, realTimeBarsOptions
        )
        return bars

    def cancelRealTimeBars(self, bars: RealTimeBarList):
        """
        Cancel the realtime bars subscription.

        Args:
            bars: The bar list that was obtained from ``reqRealTimeBars``.
        """
        self.client.cancelRealTimeBars(bars.reqId)
        self.wrapper.endSubscription(bars)

    def reqHistoricalData(
        self,
        contract: Contract,
        endDateTime: Union[datetime.datetime, datetime.date, str, None],
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRTH: bool,
        formatDate: int = 1,
        keepUpToDate: bool = False,
        chartOptions: list[TagValue] = [],
        timeout: float = 60,
    ) -> BarDataList:
        """
        Request historical bar data.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/historical_bars.html

        Args:
            contract: Contract of interest.
            endDateTime: Can be set to '' to indicate the current time,
                or it can be given as a datetime.date or datetime.datetime,
                or it can be given as a string in 'yyyyMMdd HH:mm:ss' format.
                If no timezone is given then the TWS login timezone is used.
            durationStr: Time span of all the bars. Examples:
                '60 S', '30 D', '13 W', '6 M', '10 Y'.
            barSizeSetting: Time period of one bar. Must be one of:
                '1 secs', '5 secs', '10 secs' 15 secs', '30 secs',
                '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
                '20 mins', '30 mins',
                '1 hour', '2 hours', '3 hours', '4 hours', '8 hours',
                '1 day', '1 week', '1 month'.
            whatToShow: Specifies the source for constructing bars.
                Must be one of:
                'TRADES', 'MIDPOINT', 'BID', 'ASK', 'BID_ASK',
                'ADJUSTED_LAST', 'HISTORICAL_VOLATILITY',
                'OPTION_IMPLIED_VOLATILITY', 'REBATE_RATE', 'FEE_RATE',
                'YIELD_BID', 'YIELD_ASK', 'YIELD_BID_ASK', 'YIELD_LAST'.
                For 'SCHEDULE' use :meth:`.reqHistoricalSchedule`.
            useRTH: If True then only show data from within Regular
                Trading Hours, if False then show all data.
            formatDate: For an intraday request setting to 2 will cause
                the returned date fields to be timezone-aware
                datetime.datetime with UTC timezone, instead of local timezone
                as used by TWS.
            keepUpToDate: If True then a realtime subscription is started
                to keep the bars updated; ``endDateTime`` must be set
                empty ('') then.
            chartOptions: Unknown.
            timeout: Timeout in seconds after which to cancel the request
                and return an empty bar series. Set to ``0`` to wait
                indefinitely.
        """
        return self._run(
            self.reqHistoricalDataAsync(
                contract,
                endDateTime,
                durationStr,
                barSizeSetting,
                whatToShow,
                useRTH,
                formatDate,
                keepUpToDate,
                chartOptions,
                timeout,
            )
        )

    def cancelHistoricalData(self, bars: BarDataList):
        """
        Cancel the update subscription for the historical bars.

        Args:
            bars: The bar list that was obtained from ``reqHistoricalData``
                with a keepUpToDate subscription.

        """
        self.client.cancelHistoricalData(bars.reqId)
        self.wrapper.endSubscription(bars)

    def reqHistoricalSchedule(
        self,
        contract: Contract,
        numDays: int,
        endDateTime: Union[datetime.datetime, datetime.date, str, None] = "",
        useRTH: bool = True,
    ) -> HistoricalSchedule:
        """
        Request historical schedule.

        This method is blocking.

        Args:
            contract: Contract of interest.
            numDays: Number of days.
            endDateTime: Can be set to '' to indicate the current time,
                or it can be given as a datetime.date or datetime.datetime,
                or it can be given as a string in 'yyyyMMdd HH:mm:ss' format.
                If no timezone is given then the TWS login timezone is used.
            useRTH: If True then show schedule for Regular Trading Hours,
                if False then for extended hours.
        """
        return self._run(
            self.reqHistoricalScheduleAsync(contract, numDays, endDateTime, useRTH)
        )

    def reqHistoricalTicks(
        self,
        contract: Contract,
        startDateTime: Union[str, datetime.date],
        endDateTime: Union[str, datetime.date],
        numberOfTicks: int,
        whatToShow: str,
        useRth: bool,
        ignoreSize: bool = False,
        miscOptions: list[TagValue] = [],
    ) -> List:
        """
        Request historical ticks. The time resolution of the ticks
        is one second.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/historical_time_and_sales.html

        Args:
            contract: Contract to query.
            startDateTime: Can be given as a datetime.date or
                datetime.datetime, or it can be given as a string in
                'yyyyMMdd HH:mm:ss' format.
                If no timezone is given then the TWS login timezone is used.
            endDateTime: One of ``startDateTime`` or ``endDateTime`` can
                be given, the other must be blank.
            numberOfTicks: Number of ticks to request (1000 max). The actual
                result can contain a bit more to accommodate all ticks in
                the latest second.
            whatToShow: One of 'Bid_Ask', 'Midpoint' or 'Trades'.
            useRTH: If True then only show data from within Regular
                Trading Hours, if False then show all data.
            ignoreSize: Ignore bid/ask ticks that only update the size.
            miscOptions: Unknown.
        """
        return self._run(
            self.reqHistoricalTicksAsync(
                contract,
                startDateTime,
                endDateTime,
                numberOfTicks,
                whatToShow,
                useRth,
                ignoreSize,
                miscOptions,
            )
        )

    def reqMarketDataType(self, marketDataType: int):
        """
        Set the market data type used for :meth:`.reqMktData`.

        Args:
            marketDataType: One of:

                * 1 = Live
                * 2 = Frozen
                * 3 = Delayed
                * 4 = Delayed frozen

        https://interactivebrokers.github.io/tws-api/market_data_type.html
        """
        self.client.reqMarketDataType(marketDataType)

    def reqHeadTimeStamp(
        self, contract: Contract, whatToShow: str, useRTH: bool, formatDate: int = 1
    ) -> datetime.datetime:
        """
        Get the datetime of earliest available historical data
        for the contract.

        Args:
            contract: Contract of interest.
            useRTH: If True then only show data from within Regular
                Trading Hours, if False then show all data.
            formatDate: If set to 2 then the result is returned as a
                timezone-aware datetime.datetime with UTC timezone.
        """
        return self._run(
            self.reqHeadTimeStampAsync(contract, whatToShow, useRTH, formatDate)
        )

    def reqMktData(
        self,
        contract: Contract,
        genericTickList: str = "",
        snapshot: bool = False,
        regulatorySnapshot: bool = False,
        mktDataOptions: list[TagValue] = [],
    ) -> Ticker:
        """
        Subscribe to tick data or request a snapshot.
        Returns the Ticker that holds the market data. The ticker will
        initially be empty and gradually (after a couple of seconds)
        be filled.

        https://interactivebrokers.github.io/tws-api/md_request.html

        Args:
            contract: Contract of interest.
            genericTickList: Comma separated IDs of desired
                generic ticks that will cause corresponding Ticker fields
                to be filled:

                =====  ================================================
                ID     Ticker fields
                =====  ================================================
                100    ``putVolume``, ``callVolume`` (for options)
                101    ``putOpenInterest``, ``callOpenInterest`` (for options)
                104    ``histVolatility`` (for options)
                105    ``avOptionVolume`` (for options)
                106    ``impliedVolatility`` (for options)
                162    ``indexFuturePremium``
                165    ``low13week``, ``high13week``, ``low26week``,
                       ``high26week``, ``low52week``, ``high52week``,
                       ``avVolume``
                221    ``markPrice``
                225    ``auctionVolume``, ``auctionPrice``,
                       ``auctionImbalance``
                233    ``last``, ``lastSize``, ``rtVolume``, ``rtTime``,
                       ``vwap`` (Time & Sales)
                236    ``shortableShares``
                258    ``fundamentalRatios`` (of type
                       :class:`ib_async.objects.FundamentalRatios`)
                293    ``tradeCount``
                294    ``tradeRate``
                295    ``volumeRate``
                375    ``rtTradeVolume``
                411    ``rtHistVolatility``
                456    ``dividends`` (of type
                       :class:`ib_async.objects.Dividends`)
                588    ``futuresOpenInterest``
                =====  ================================================

            snapshot: If True then request a one-time snapshot, otherwise
                subscribe to a stream of realtime tick data.
            regulatorySnapshot: Request NBBO snapshot (may incur a fee).
            mktDataOptions: Unknown
        """
        reqId = self.client.getReqId()
        ticker = self.wrapper.startTicker(reqId, contract, "mktData")
        self.client.reqMktData(
            reqId,
            contract,
            genericTickList,
            snapshot,
            regulatorySnapshot,
            mktDataOptions,
        )
        return ticker

    def cancelMktData(self, contract: Contract) -> bool:
        """
        Unsubscribe from realtime streaming tick data.

        Args:
            contract: The contract of a previously subscribed ticker to unsubscribe.

        Returns:
            Returns True if cancel was successful.
            Returns False if 'contract' was not found.
        """
        ticker = self.ticker(contract)
        reqId = self.wrapper.endTicker(ticker, "mktData") if ticker else 0

        if reqId:
            self.client.cancelMktData(reqId)
            return True

        self._logger.error(f"cancelMktData: No reqId found for contract {contract}")

        return False

    def reqTickByTickData(
        self,
        contract: Contract,
        tickType: str,
        numberOfTicks: int = 0,
        ignoreSize: bool = False,
    ) -> Ticker:
        """
        Subscribe to tick-by-tick data and return the Ticker that
        holds the ticks in ticker.tickByTicks.

        https://interactivebrokers.github.io/tws-api/tick_data.html

        Args:
            contract: Contract of interest.
            tickType: One of  'Last', 'AllLast', 'BidAsk' or 'MidPoint'.
            numberOfTicks: Number of ticks or 0 for unlimited.
            ignoreSize: Ignore bid/ask ticks that only update the size.
        """
        reqId = self.client.getReqId()
        ticker = self.wrapper.startTicker(reqId, contract, tickType)

        self.client.reqTickByTickData(
            reqId, contract, tickType, numberOfTicks, ignoreSize
        )

        return ticker

    def cancelTickByTickData(self, contract: Contract, tickType: str) -> bool:
        """
        Unsubscribe from tick-by-tick data

        Args:
            contract: The contract of a previously subscribed ticker to unsubscribe.

        Returns:
            Returns True if cancel was successful.
            Returns False if 'contract' was not found.
        """
        ticker = self.ticker(contract)
        reqId = self.wrapper.endTicker(ticker, tickType) if ticker else 0

        if reqId:
            self.client.cancelTickByTickData(reqId)
            return True

        self._logger.error(f"cancelMktData: No reqId found for contract {contract}")
        return False

    def reqSmartComponents(self, bboExchange: str) -> list[SmartComponent]:
        """
        Obtain mapping from single letter codes to exchange names.

        Note: The exchanges must be open when using this request, otherwise an
        empty list is returned.
        """
        return self._run(self.reqSmartComponentsAsync(bboExchange))

    def reqMktDepthExchanges(self) -> list[DepthMktDataDescription]:
        """
        Get those exchanges that have have multiple market makers
        (and have ticks returned with marketMaker info).
        """
        return self._run(self.reqMktDepthExchangesAsync())

    def reqMktDepth(
        self,
        contract: Contract,
        numRows: int = 5,
        isSmartDepth: bool = False,
        mktDepthOptions=None,
    ) -> Ticker:
        """
        Subscribe to market depth data (a.k.a. DOM, L2 or order book).

        https://interactivebrokers.github.io/tws-api/market_depth.html

        Args:
            contract: Contract of interest.
            numRows: Number of depth level on each side of the order book
                (5 max).
            isSmartDepth: Consolidate the order book across exchanges.
            mktDepthOptions: Unknown.

        Returns:
            The Ticker that holds the market depth in ``ticker.domBids``
            and ``ticker.domAsks`` and the list of MktDepthData in
            ``ticker.domTicks``.
        """
        reqId = self.client.getReqId()
        ticker = self.wrapper.startTicker(reqId, contract, "mktDepth")
        ticker.domBids.clear()
        ticker.domAsks.clear()
        ticker.domBidsDict.clear()
        ticker.domAsksDict.clear()
        self.client.reqMktDepth(reqId, contract, numRows, isSmartDepth, mktDepthOptions)
        return ticker

    def cancelMktDepth(self, contract: Contract, isSmartDepth=False):
        """
        Unsubscribe from market depth data.

        Args:
            contract: The exact contract object that was used to
                subscribe with.
        """
        ticker = self.ticker(contract)
        reqId = self.wrapper.endTicker(ticker, "mktDepth") if ticker else 0
        if ticker and reqId:
            self.client.cancelMktDepth(reqId, isSmartDepth)

            # clear market depth state from live ticker since it is not longer
            # being updated after the cancel request.
            ticker.domBids.clear()
            ticker.domAsks.clear()
            ticker.domBidsDict.clear()
            ticker.domAsksDict.clear()
        else:
            self._logger.error(
                f"cancelMktDepth: No reqId found for contract {contract}"
            )

    def reqHistogramData(
        self, contract: Contract, useRTH: bool, period: str
    ) -> list[HistogramData]:
        """
        Request histogram data.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/histograms.html

        Args:
            contract: Contract to query.
            useRTH: If True then only show data from within Regular
                Trading Hours, if False then show all data.
            period: Period of which data is being requested, for example
                '3 days'.
        """
        return self._run(self.reqHistogramDataAsync(contract, useRTH, period))

    def reqFundamentalData(
        self,
        contract: Contract,
        reportType: str,
        fundamentalDataOptions: list[TagValue] = [],
    ) -> str:
        """
        Get fundamental data of a contract in XML format.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/fundamentals.html

        Args:
            contract: Contract to query.
            reportType:

                * 'ReportsFinSummary': Financial summary
                * 'ReportsOwnership': Company's ownership
                * 'ReportSnapshot': Company's financial overview
                * 'ReportsFinStatements': Financial Statements
                * 'RESC': Analyst Estimates
                * 'CalendarReport': Company's calendar
            fundamentalDataOptions: Unknown
        """
        return self._run(
            self.reqFundamentalDataAsync(contract, reportType, fundamentalDataOptions)
        )

    def reqScannerData(
        self,
        subscription: ScannerSubscription,
        scannerSubscriptionOptions: list[TagValue] = [],
        scannerSubscriptionFilterOptions: list[TagValue] = [],
    ) -> ScanDataList:
        """
        Do a blocking market scan by starting a subscription and canceling it
        after the initial list of results are in.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/market_scanners.html

        Args:
            subscription: Basic filters.
            scannerSubscriptionOptions: Unknown.
            scannerSubscriptionFilterOptions: Advanced generic filters.
        """
        return self._run(
            self.reqScannerDataAsync(
                subscription,
                scannerSubscriptionOptions,
                scannerSubscriptionFilterOptions,
            )
        )

    def reqScannerSubscription(
        self,
        subscription: ScannerSubscription,
        scannerSubscriptionOptions: list[TagValue] = [],
        scannerSubscriptionFilterOptions: list[TagValue] = [],
    ) -> ScanDataList:
        """
        Subscribe to market scan data.

        https://interactivebrokers.github.io/tws-api/market_scanners.html

        Args:
            subscription: What to scan for.
            scannerSubscriptionOptions: Unknown.
            scannerSubscriptionFilterOptions: Unknown.
        """
        reqId = self.client.getReqId()
        dataList = ScanDataList()
        dataList.reqId = reqId
        dataList.subscription = subscription
        dataList.scannerSubscriptionOptions = scannerSubscriptionOptions or []
        dataList.scannerSubscriptionFilterOptions = (
            scannerSubscriptionFilterOptions or []
        )
        self.wrapper.startSubscription(reqId, dataList)
        self.client.reqScannerSubscription(
            reqId,
            subscription,
            scannerSubscriptionOptions,
            scannerSubscriptionFilterOptions,
        )
        return dataList

    def cancelScannerSubscription(self, dataList: ScanDataList):
        """
        Cancel market data subscription.

        https://interactivebrokers.github.io/tws-api/market_scanners.html

        Args:
            dataList: The scan data list that was obtained from
                :meth:`.reqScannerSubscription`.
        """
        self.client.cancelScannerSubscription(dataList.reqId)
        self.wrapper.endSubscription(dataList)

    def reqScannerParameters(self) -> str:
        """
        Requests an XML list of scanner parameters.

        This method is blocking.
        """
        return self._run(self.reqScannerParametersAsync())

    def calculateImpliedVolatility(
        self,
        contract: Contract,
        optionPrice: float,
        underPrice: float,
        implVolOptions: list[TagValue] = [],
    ) -> OptionComputation:
        """
        Calculate the volatility given the option price.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/option_computations.html

        Args:
            contract: Option contract.
            optionPrice: Option price to use in calculation.
            underPrice: Price of the underlier to use in calculation
            implVolOptions: Unknown
        """
        return self._run(
            self.calculateImpliedVolatilityAsync(
                contract, optionPrice, underPrice, implVolOptions
            )
        )

    def calculateOptionPrice(
        self,
        contract: Contract,
        volatility: float,
        underPrice: float,
        optPrcOptions: list[TagValue] = [],
    ) -> OptionComputation:
        """
        Calculate the option price given the volatility.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/option_computations.html

        Args:
            contract: Option contract.
            volatility: Option volatility to use in calculation.
            underPrice: Price of the underlier to use in calculation
            implVolOptions: Unknown
        """
        return self._run(
            self.calculateOptionPriceAsync(
                contract, volatility, underPrice, optPrcOptions
            )
        )

    def reqSecDefOptParams(
        self,
        underlyingSymbol: str,
        futFopExchange: str,
        underlyingSecType: str,
        underlyingConId: int,
    ) -> list[OptionChain]:
        """
        Get the option chain.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/options.html

        Args:
            underlyingSymbol: Symbol of underlier contract.
            futFopExchange: Exchange (only for ``FuturesOption``, otherwise
                leave blank).
            underlyingSecType: The type of the underlying security, like
                'STK' or 'FUT'.
            underlyingConId: conId of the underlying contract.
        """
        return self._run(
            self.reqSecDefOptParamsAsync(
                underlyingSymbol, futFopExchange, underlyingSecType, underlyingConId
            )
        )

    def exerciseOptions(
        self,
        contract: Contract,
        exerciseAction: int,
        exerciseQuantity: int,
        account: str,
        override: int,
    ):
        """
        Exercise an options contract.

        https://interactivebrokers.github.io/tws-api/options.html

        Args:
            contract: The option contract to be exercised.
            exerciseAction:
                * 1 = exercise the option
                * 2 = let the option lapse
            exerciseQuantity: Number of contracts to be exercised.
            account: Destination account.
            override:
                * 0 = no override
                * 1 = override the system's natural action
        """
        reqId = self.client.getReqId()
        self.client.exerciseOptions(
            reqId, contract, exerciseAction, exerciseQuantity, account, override
        )

    def reqNewsProviders(self) -> list[NewsProvider]:
        """
        Get a list of news providers.

        This method is blocking.
        """
        return self._run(self.reqNewsProvidersAsync())

    def reqNewsArticle(
        self, providerCode: str, articleId: str, newsArticleOptions: list[TagValue] = []
    ) -> NewsArticle:
        """
        Get the body of a news article.

        This method is blocking.

        https://interactivebrokers.github.io/tws-api/news.html

        Args:
            providerCode: Code indicating news provider, like 'BZ' or 'FLY'.
            articleId: ID of the specific article.
            newsArticleOptions: Unknown.
        """
        return self._run(
            self.reqNewsArticleAsync(providerCode, articleId, newsArticleOptions)
        )

    def reqHistoricalNews(
        self,
        conId: int,
        providerCodes: str,
        startDateTime: Union[str, datetime.date],
        endDateTime: Union[str, datetime.date],
        totalResults: int,
        historicalNewsOptions: list[TagValue] = [],
    ) -> HistoricalNews:
        """
        Get historical news headline.

        https://interactivebrokers.github.io/tws-api/news.html

        This method is blocking.

        Args:
            conId: Search news articles for contract with this conId.
            providerCodes: A '+'-separated list of provider codes, like
                'BZ+FLY'.
            startDateTime: The (exclusive) start of the date range.
                Can be given as a datetime.date or datetime.datetime,
                or it can be given as a string in 'yyyyMMdd HH:mm:ss' format.
                If no timezone is given then the TWS login timezone is used.
            endDateTime: The (inclusive) end of the date range.
                Can be given as a datetime.date or datetime.datetime,
                or it can be given as a string in 'yyyyMMdd HH:mm:ss' format.
                If no timezone is given then the TWS login timezone is used.
            totalResults: Maximum number of headlines to fetch (300 max).
            historicalNewsOptions: Unknown.
        """
        return self._run(
            self.reqHistoricalNewsAsync(
                conId,
                providerCodes,
                startDateTime,
                endDateTime,
                totalResults,
                historicalNewsOptions,
            )
        )

    def reqNewsBulletins(self, allMessages: bool):
        """
        Subscribe to IB news bulletins.

        https://interactivebrokers.github.io/tws-api/news.html

        Args:
            allMessages: If True then fetch all messages for the day.
        """
        self.client.reqNewsBulletins(allMessages)

    def cancelNewsBulletins(self):
        """Cancel subscription to IB news bulletins."""
        self.client.cancelNewsBulletins()

    def requestFA(self, faDataType: int):
        """
        Requests to change the FA configuration.

        This method is blocking.

        Args:
            faDataType:

                * 1 = Groups: Offer traders a way to create a group of
                  accounts and apply a single allocation method to all
                  accounts in the group.
                * 2 = Profiles: Let you allocate shares on an
                  account-by-account basis using a predefined calculation
                  value.
                * 3 = Account Aliases: Let you easily identify the accounts
                  by meaningful names rather than account numbers.
        """
        return self._run(self.requestFAAsync(faDataType))

    def replaceFA(self, faDataType: int, xml: str):
        """
        Replaces Financial Advisor's settings.

        Args:
            faDataType: See :meth:`.requestFA`.
            xml: The XML-formatted configuration string.
        """
        reqId = self.client.getReqId()
        self.client.replaceFA(reqId, faDataType, xml)

    def reqWshMetaData(self):
        """
        Request Wall Street Horizon metadata.

        https://interactivebrokers.github.io/tws-api/fundamentals.html
        """
        if self.wrapper.wshMetaReqId:
            self._logger.warning("reqWshMetaData already active")
        else:
            reqId = self.client.getReqId()
            self.wrapper.wshMetaReqId = reqId
            self.client.reqWshMetaData(reqId)

    def cancelWshMetaData(self):
        """Cancel WSH metadata."""
        reqId = self.wrapper.wshMetaReqId
        if not reqId:
            self._logger.warning("reqWshMetaData not active")
        else:
            self.client.cancelWshMetaData(reqId)
            self.wrapper.wshMetaReqId = 0

    def reqWshEventData(self, data: WshEventData):
        """
        Request Wall Street Horizon event data.

        :meth:`.reqWshMetaData` must have been called first before using this
        method.

        Args:
            data: Filters for selecting the corporate event data.

        https://interactivebrokers.github.io/tws-api/wshe_filters.html
        """
        if self.wrapper.wshEventReqId:
            self._logger.warning("reqWshEventData already active")
        else:
            reqId = self.client.getReqId()
            self.wrapper.wshEventReqId = reqId
            self.client.reqWshEventData(reqId, data)

    def cancelWshEventData(self):
        """Cancel active WHS event data."""
        reqId = self.wrapper.wshEventReqId
        if not reqId:
            self._logger.warning("reqWshEventData not active")
        else:
            self.client.cancelWshEventData(reqId)
            self.wrapper.wshEventReqId = 0

    def getWshMetaData(self) -> str:
        """
        Blocking convenience method that returns the WSH metadata (that is
        the available filters and event types) as a JSON string.

        Please note that a `Wall Street Horizon subscription
        <https://www.wallstreethorizon.com/interactive-brokers>`_
        is required.

        .. code-block:: python

            # Get the list of available filters and event types:
            meta = ib.getWshMetaData()
            print(meta)
        """
        return self._run(self.getWshMetaDataAsync())

    def getWshEventData(self, data: WshEventData) -> str:
        """
        Blocking convenience method that returns the WSH event data as
        a JSON string.
        :meth:`.getWshMetaData` must have been called first before using this
        method.

        Please note that a  `Wall Street Horizon subscription
        <https://www.wallstreethorizon.com/interactive-brokers>`_
        is required.

        .. code-block:: python

            # For IBM (with conId=8314) query the:
            #   - Earnings Dates (wshe_ed)
            #   - Board of Directors meetings (wshe_bod)
            data = WshEventData(
                filter = '''{
                  "country": "All",
                  "watchlist": ["8314"],
                  "limit_region": 10,
                  "limit": 10,
                  "wshe_ed": "true",
                  "wshe_bod": "true"
                }''')
            events = ib.getWshEventData(data)
            print(events)
        """
        return self._run(self.getWshEventDataAsync(data))

    def reqUserInfo(self) -> str:
        """Get the White Branding ID of the user."""
        return self._run(self.reqUserInfoAsync())

    # now entering the parallel async universe

    async def connectAsync(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        clientId: int = 1,
        timeout: Optional[float] = 4,
        readonly: bool = False,
        account: str = "",
        raiseSyncErrors: bool = False,
        fetchFields: StartupFetch = StartupFetchALL,
    ):
        clientId = int(clientId)
        self.wrapper.clientId = clientId
        timeout = timeout or None
        try:
            # establish API connection
            await self.client.connectAsync(host, port, clientId, timeout)

            # autobind manual orders
            if clientId == 0:
                self.reqAutoOpenOrders(True)

            accounts = self.client.getAccounts()
            if not account and len(accounts) == 1:
                account = accounts[0]

            # prepare initializing requests
            # name -> request
            reqs: dict[str, Awaitable[Any]] = {}
            reqs["positions"] = self.reqPositionsAsync()

            if not readonly:
                if fetchFields & StartupFetch.ORDERS_OPEN:
                    reqs["open orders"] = self.reqOpenOrdersAsync()

            if not readonly and self.client.serverVersion() >= 150:
                if fetchFields & StartupFetch.ORDERS_COMPLETE:
                    reqs["completed orders"] = self.reqCompletedOrdersAsync(False)

            if account:
                if fetchFields & StartupFetch.ACCOUNT_UPDATES:
                    reqs["account updates"] = self.reqAccountUpdatesAsync(account)

            # run initializing requests concurrently and log if any times out
            tasks = [asyncio.wait_for(req, timeout) for req in reqs.values()]
            errors = []
            resps = await asyncio.gather(*tasks, return_exceptions=True)
            for name, resp in zip(reqs, resps):
                if isinstance(resp, asyncio.TimeoutError):
                    msg = f"{name} request timed out"
                    errors.append(msg)
                    self._logger.error(msg)

            # To get portfolios for multiple accounts we have to subscribe to each
            # account serially to ensure all data is loaded. We have to do it serially
            # because IB API sends back a generic accountDownloadEnd signal when it
            # finishes sending the data for the first account, so we cannot subscribe
            # to multiple accounts at once.
            if len(accounts) <= self.MaxSyncedSubAccounts:
                for acc in accounts:
                    try:
                        await asyncio.wait_for(
                            self.reqAccountUpdatesAsync(acc), timeout
                        )
                    except asyncio.TimeoutError:
                        msg = f"reqAccountUpdatesAsync for {acc} timed out"
                        errors.append(msg)
                        self._logger.error(msg)
                self._logger.info("Finished fetching all portfolio data.")

            # the request for executions must come after all orders are in
            if fetchFields & StartupFetch.EXECUTIONS:
                try:
                    await asyncio.wait_for(self.reqExecutionsAsync(), timeout)
                except asyncio.TimeoutError:
                    msg = "executions request timed out"
                    errors.append(msg)
                    self._logger.error(msg)

            if raiseSyncErrors and len(errors) > 0:
                raise ConnectionError(errors)

            # final check if socket is still ready
            if not self.client.isReady():
                raise ConnectionError("Socket connection broken while connecting")

            self._logger.info("Synchronization complete")
            self.connectedEvent.emit()
        except BaseException:
            self.disconnect()
            raise

        return self

    async def qualifyContractsAsync(
        self, *contracts: Contract, returnAll: bool = False
    ) -> list[Contract | list[Contract | None] | None]:
        """Looks up all contract details, but only returns matching Contract objects.

        If 'returnAll' is True, instead of returning 'None' on an ambiguous contract request,
        the return slot will have a list of the matching contracts. Previously the conflicts
        were only sent to the log, which isn't useful if you are logging to a file and not watching
        immediately.

        Note: return value has elements in same position as input request. If a contract
              cannot be qualified (bad values, ambiguous), the return value for the contract
              position in the result is None.
        """
        detailsLists = await asyncio.gather(
            *[self.reqContractDetailsAsync(c) for c in contracts]
        )

        # self._logger.warning(f"Got details: {detailsLists=}")

        result: list[Contract | list[Contract | None] | None] = []
        for contract, detailsList in zip(contracts, detailsLists):
            if not detailsList:
                self._logger.warning(f"Unknown contract: {contract}")
                result.append(None)
            elif len(detailsList) > 1:
                # BUG FIX:
                #  - IBKR is returning EC _and_ FOP contracts for only FOP requests,
                #    which is clearly incorrect, so now if an input request has `secType`
                #    defined, we only return matching `secType` contracts.
                if contract.secType:
                    possibles = [
                        details.contract
                        for details in detailsList
                        if contract.secType == details.contract.secType  # type: ignore
                    ]

                    # if our match instrument type filter resolved to only _one_ matching
                    # contract, then we found a single usable result to add.
                    if len(possibles) == 1:
                        c = possibles[0]
                        if contract.exchange == "SMART":
                            # Allow contracts to become more generic if SMART requested as input
                            c.exchange = contract.exchange  # type: ignore

                        util.dataclassUpdate(contract, c)
                        result.append(contract)
                        continue
                else:
                    # else, return all matches if no specific secType requested
                    possibles = [details.contract for details in detailsList]

                self._logger.warning(
                    f"Ambiguous contract: {contract}, possibles are {possibles}"
                )

                if returnAll:
                    result.append(possibles)
                else:
                    result.append(None)
            else:
                c = detailsList[0].contract
                assert c
                if contract.exchange == "SMART":
                    # overwriting 'SMART' exchange can create invalid contract
                    c.exchange = contract.exchange

                util.dataclassUpdate(contract, c)
                result.append(contract)

        return result

    async def reqTickersAsync(
        self, *contracts: Contract, regulatorySnapshot: bool = False
    ) -> list[Ticker]:
        futures = []
        tickers = []
        reqIds = []
        for contract in contracts:
            reqId = self.client.getReqId()
            reqIds.append(reqId)
            future = self.wrapper.startReq(reqId, contract)
            futures.append(future)
            ticker = self.wrapper.startTicker(reqId, contract, "snapshot")
            tickers.append(ticker)
            self.client.reqMktData(reqId, contract, "", True, regulatorySnapshot, [])

        await asyncio.gather(*futures)

        for ticker in tickers:
            self.wrapper.endTicker(ticker, "snapshot")

        return tickers

    def whatIfOrderAsync(
        self, contract: Contract, order: Order
    ) -> Awaitable[OrderState]:
        whatIfOrder = copy.copy(order)
        whatIfOrder.whatIf = True
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId, contract)
        self.client.placeOrder(reqId, contract, whatIfOrder)
        return future

    def reqCurrentTimeAsync(self) -> Awaitable[datetime.datetime]:
        future = self.wrapper.startReq("currentTime")
        self.client.reqCurrentTime()
        return future

    def reqAccountUpdatesAsync(self, account: str) -> Awaitable[None]:
        future = self.wrapper.startReq("accountValues")
        self.client.reqAccountUpdates(True, account)
        return future

    def reqAccountUpdatesMultiAsync(
        self, account: str, modelCode: str = ""
    ) -> Awaitable[None]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId)
        self.client.reqAccountUpdatesMulti(reqId, account, modelCode, False)
        return future

    async def accountSummaryAsync(self, account: str = "") -> list[AccountValue]:
        if not self.wrapper.acctSummary:
            # loaded on demand since it takes ca. 250 ms
            await self.reqAccountSummaryAsync()

        if account:
            return [
                v for v in self.wrapper.acctSummary.values() if v.account == account
            ]

        return list(self.wrapper.acctSummary.values())

    def reqAccountSummaryAsync(self) -> Awaitable[None]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId)
        tags = (
            "AccountType,NetLiquidation,TotalCashValue,SettledCash,"
            "AccruedCash,BuyingPower,EquityWithLoanValue,"
            "PreviousDayEquityWithLoanValue,GrossPositionValue,RegTEquity,"
            "RegTMargin,SMA,InitMarginReq,MaintMarginReq,AvailableFunds,"
            "ExcessLiquidity,Cushion,FullInitMarginReq,FullMaintMarginReq,"
            "FullAvailableFunds,FullExcessLiquidity,LookAheadNextChange,"
            "LookAheadInitMarginReq,LookAheadMaintMarginReq,"
            "LookAheadAvailableFunds,LookAheadExcessLiquidity,"
            "HighestSeverity,DayTradesRemaining,DayTradesRemainingT+1,"
            "DayTradesRemainingT+2,DayTradesRemainingT+3,"
            "DayTradesRemainingT+4,Leverage,$LEDGER:ALL"
        )
        self.client.reqAccountSummary(reqId, "All", tags)
        return future

    def reqOpenOrdersAsync(self) -> Awaitable[list[Trade]]:
        future = self.wrapper.startReq("openOrders")
        self.client.reqOpenOrders()
        return future

    def reqAllOpenOrdersAsync(self) -> Awaitable[list[Trade]]:
        future = self.wrapper.startReq("openOrders")
        self.client.reqAllOpenOrders()
        return future

    def reqCompletedOrdersAsync(self, apiOnly: bool) -> Awaitable[list[Trade]]:
        future = self.wrapper.startReq("completedOrders")
        self.client.reqCompletedOrders(apiOnly)
        return future

    def reqExecutionsAsync(
        self, execFilter: Optional[ExecutionFilter] = None
    ) -> Awaitable[list[Fill]]:
        execFilter = execFilter or ExecutionFilter()
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId)
        self.client.reqExecutions(reqId, execFilter)
        return future

    def reqPositionsAsync(self) -> Awaitable[list[Position]]:
        future = self.wrapper.startReq("positions")
        self.client.reqPositions()
        return future

    def reqPositionsMultiAsync(
        self, account: str = "", modelCode: str = ""
    ) -> Awaitable[None]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId)
        self.client.reqPositionsMulti(reqId, account, modelCode)
        return future

    def reqContractDetailsAsync(
        self, contract: Contract
    ) -> Awaitable[list[ContractDetails]]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId, contract)
        self.client.reqContractDetails(reqId, contract)
        return future

    async def reqMatchingSymbolsAsync(
        self, pattern: str
    ) -> Optional[list[ContractDescription]]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId)
        self.client.reqMatchingSymbols(reqId, pattern)
        try:
            await asyncio.wait_for(future, 4)
            return future.result()
        except asyncio.TimeoutError:
            self._logger.error("reqMatchingSymbolsAsync: Timeout")
            return None

    async def reqMarketRuleAsync(
        self, marketRuleId: int
    ) -> Optional[list[PriceIncrement]]:
        future = self.wrapper.startReq(f"marketRule-{marketRuleId}")
        try:
            self.client.reqMarketRule(marketRuleId)
            await asyncio.wait_for(future, 1)
            return future.result()
        except asyncio.TimeoutError:
            self._logger.error("reqMarketRuleAsync: Timeout")
            return None

    async def reqHistoricalDataAsync(
        self,
        contract: Contract,
        endDateTime: Union[datetime.datetime, datetime.date, str, None],
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRTH: bool,
        formatDate: int = 1,
        keepUpToDate: bool = False,
        chartOptions: list[TagValue] = [],
        timeout: float = 60,
    ) -> BarDataList:
        reqId = self.client.getReqId()
        bars = BarDataList()
        bars.reqId = reqId
        bars.contract = contract
        bars.endDateTime = endDateTime
        bars.durationStr = durationStr
        bars.barSizeSetting = barSizeSetting
        bars.whatToShow = whatToShow
        bars.useRTH = useRTH
        bars.formatDate = formatDate
        bars.keepUpToDate = keepUpToDate
        bars.chartOptions = chartOptions or []
        future = self.wrapper.startReq(reqId, contract, container=bars)
        if keepUpToDate:
            self.wrapper.startSubscription(reqId, bars, contract)
        end = util.formatIBDatetime(endDateTime)
        self.client.reqHistoricalData(
            reqId,
            contract,
            end,
            durationStr,
            barSizeSetting,
            whatToShow,
            useRTH,
            formatDate,
            keepUpToDate,
            chartOptions,
        )
        task = asyncio.wait_for(future, timeout) if timeout else future
        try:
            await task
        except asyncio.TimeoutError:
            self.client.cancelHistoricalData(reqId)
            self._logger.warning(f"reqHistoricalData: Timeout for {contract}")
            bars.clear()

        return bars

    def reqHistoricalScheduleAsync(
        self,
        contract: Contract,
        numDays: int,
        endDateTime: Union[datetime.datetime, datetime.date, str, None] = "",
        useRTH: bool = True,
    ) -> Awaitable[HistoricalSchedule]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId, contract)
        end = util.formatIBDatetime(endDateTime)
        self.client.reqHistoricalData(
            reqId,
            contract,
            end,
            f"{numDays} D",
            "1 day",
            "SCHEDULE",
            useRTH,
            1,
            False,
            None,
        )

        return future

    def reqHistoricalTicksAsync(
        self,
        contract: Contract,
        startDateTime: Union[str, datetime.date],
        endDateTime: Union[str, datetime.date],
        numberOfTicks: int,
        whatToShow: str,
        useRth: bool,
        ignoreSize: bool = False,
        miscOptions: list[TagValue] = [],
    ) -> Awaitable[List]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId, contract)
        start = util.formatIBDatetime(startDateTime)
        end = util.formatIBDatetime(endDateTime)
        self.client.reqHistoricalTicks(
            reqId,
            contract,
            start,
            end,
            numberOfTicks,
            whatToShow,
            useRth,
            ignoreSize,
            miscOptions,
        )
        return future

    async def reqHeadTimeStampAsync(
        self, contract: Contract, whatToShow: str, useRTH: bool, formatDate: int
    ) -> datetime.datetime:
        reqId = self.client.getReqId()

        future = self.wrapper.startReq(reqId, contract)
        self.client.reqHeadTimeStamp(reqId, contract, whatToShow, useRTH, formatDate)
        await future

        self.client.cancelHeadTimeStamp(reqId)
        return future.result()

    def reqSmartComponentsAsync(self, bboExchange):
        reqId = self.client.getReqId()

        future = self.wrapper.startReq(reqId)
        self.client.reqSmartComponents(reqId, bboExchange)
        return future

    def reqMktDepthExchangesAsync(self) -> Awaitable[list[DepthMktDataDescription]]:
        future = self.wrapper.startReq("mktDepthExchanges")
        self.client.reqMktDepthExchanges()
        return future

    def reqHistogramDataAsync(
        self, contract: Contract, useRTH: bool, period: str
    ) -> Awaitable[list[HistogramData]]:
        reqId = self.client.getReqId()

        future = self.wrapper.startReq(reqId, contract)
        self.client.reqHistogramData(reqId, contract, useRTH, period)
        return future

    def reqFundamentalDataAsync(
        self,
        contract: Contract,
        reportType: str,
        fundamentalDataOptions: list[TagValue] = [],
    ) -> Awaitable[str]:
        reqId = self.client.getReqId()

        future = self.wrapper.startReq(reqId, contract)
        self.client.reqFundamentalData(
            reqId, contract, reportType, fundamentalDataOptions
        )
        return future

    async def reqScannerDataAsync(
        self,
        subscription: ScannerSubscription,
        scannerSubscriptionOptions: list[TagValue] = [],
        scannerSubscriptionFilterOptions: list[TagValue] = [],
    ) -> ScanDataList:
        dataList = self.reqScannerSubscription(
            subscription,
            scannerSubscriptionOptions or [],
            scannerSubscriptionFilterOptions or [],
        )

        future = self.wrapper.startReq(dataList.reqId, container=dataList)
        await future

        self.client.cancelScannerSubscription(dataList.reqId)
        return future.result()

    def reqScannerParametersAsync(self) -> Awaitable[str]:
        future = self.wrapper.startReq("scannerParams")
        self.client.reqScannerParameters()
        return future

    async def calculateImpliedVolatilityAsync(
        self,
        contract: Contract,
        optionPrice: float,
        underPrice: float,
        implVolOptions: list[TagValue] = [],
    ) -> Optional[OptionComputation]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId, contract)
        self.client.calculateImpliedVolatility(
            reqId, contract, optionPrice, underPrice, implVolOptions
        )
        try:
            await asyncio.wait_for(future, 4)
            return future.result()
        except asyncio.TimeoutError:
            self._logger.error("calculateImpliedVolatilityAsync: Timeout")
            return None
        finally:
            self.client.cancelCalculateImpliedVolatility(reqId)

    async def calculateOptionPriceAsync(
        self,
        contract: Contract,
        volatility: float,
        underPrice: float,
        optPrcOptions: list[TagValue] = [],
    ) -> Optional[OptionComputation]:
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId, contract)
        self.client.calculateOptionPrice(
            reqId, contract, volatility, underPrice, optPrcOptions
        )
        try:
            await asyncio.wait_for(future, 4)
            return future.result()
        except asyncio.TimeoutError:
            self._logger.error("calculateOptionPriceAsync: Timeout")
            return None
        finally:
            self.client.cancelCalculateOptionPrice(reqId)

    def reqSecDefOptParamsAsync(
        self,
        underlyingSymbol: str,
        futFopExchange: str,
        underlyingSecType: str,
        underlyingConId: int,
    ) -> Awaitable[list[OptionChain]]:
        reqId = self.client.getReqId()

        future = self.wrapper.startReq(reqId)
        self.client.reqSecDefOptParams(
            reqId, underlyingSymbol, futFopExchange, underlyingSecType, underlyingConId
        )
        return future

    def reqNewsProvidersAsync(self) -> Awaitable[list[NewsProvider]]:
        future = self.wrapper.startReq("newsProviders")
        self.client.reqNewsProviders()
        return future

    def reqNewsArticleAsync(
        self, providerCode: str, articleId: str, newsArticleOptions: list[TagValue] = []
    ) -> Awaitable[NewsArticle]:
        reqId = self.client.getReqId()

        future = self.wrapper.startReq(reqId)
        self.client.reqNewsArticle(reqId, providerCode, articleId, newsArticleOptions)
        return future

    async def reqHistoricalNewsAsync(
        self,
        conId: int,
        providerCodes: str,
        startDateTime: Union[str, datetime.date],
        endDateTime: Union[str, datetime.date],
        totalResults: int,
        historicalNewsOptions: list[TagValue] = [],
    ) -> Optional[HistoricalNews]:
        reqId = self.client.getReqId()

        future = self.wrapper.startReq(reqId)
        start = util.formatIBDatetime(startDateTime)
        end = util.formatIBDatetime(endDateTime)
        self.client.reqHistoricalNews(
            reqId, conId, providerCodes, start, end, totalResults, historicalNewsOptions
        )
        try:
            await asyncio.wait_for(future, 4)
            return future.result()
        except asyncio.TimeoutError:
            self._logger.error("reqHistoricalNewsAsync: Timeout")
            return None

    async def requestFAAsync(self, faDataType: int):
        future = self.wrapper.startReq("requestFA")
        self.client.requestFA(faDataType)
        try:
            await asyncio.wait_for(future, 4)
            return future.result()
        except asyncio.TimeoutError:
            self._logger.error("requestFAAsync: Timeout")

    async def getWshMetaDataAsync(self) -> str:
        if self.wrapper.wshMetaReqId:
            self.cancelWshMetaData()

        self.reqWshMetaData()
        future = self.wrapper.startReq(self.wrapper.wshMetaReqId, container="")
        await future

        return future.result()

    async def getWshEventDataAsync(self, data: WshEventData) -> str:
        if self.wrapper.wshEventReqId:
            self.cancelWshEventData()

        self.reqWshEventData(data)
        future = self.wrapper.startReq(self.wrapper.wshEventReqId, container="")
        await future

        self.cancelWshEventData()
        return future.result()

    def reqUserInfoAsync(self):
        reqId = self.client.getReqId()
        future = self.wrapper.startReq(reqId)
        self.client.reqUserInfo(reqId)
        return future


if __name__ == "__main__":
    loop = util.getLoop()
    loop.set_debug(True)
    util.logToConsole(logging.DEBUG)
    ib = IB()
    ib.connect("127.0.0.1", 7497, clientId=1)
    ib.disconnect()
