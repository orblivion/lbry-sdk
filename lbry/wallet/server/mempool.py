# Copyright (c) 2016-2018, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

"""Mempool handling."""
import asyncio
import itertools
import time
import attr
import typing
from typing import Set, Optional, Callable, Awaitable
from collections import defaultdict
from prometheus_client import Histogram
from lbry.wallet.server.hash import hash_to_hex_str, hex_str_to_hash
from lbry.wallet.server.util import class_logger, chunks
from lbry.wallet.server.leveldb import UTXO
if typing.TYPE_CHECKING:
    from lbry.wallet.server.session import LBRYSessionManager


@attr.s(slots=True)
class MemPoolTx:
    prevouts = attr.ib()
    # A pair is a (hashX, value) tuple
    in_pairs = attr.ib()
    out_pairs = attr.ib()
    fee = attr.ib()
    size = attr.ib()
    raw_tx = attr.ib()


@attr.s(slots=True)
class MemPoolTxSummary:
    hash = attr.ib()
    fee = attr.ib()
    has_unconfirmed_inputs = attr.ib()


NAMESPACE = "wallet_server"
HISTOGRAM_BUCKETS = (
    .005, .01, .025, .05, .075, .1, .25, .5, .75, 1.0, 2.5, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0, 60.0, float('inf')
)
mempool_process_time_metric = Histogram(
    "processed_mempool", "Time to process mempool and notify touched addresses",
    namespace=NAMESPACE, buckets=HISTOGRAM_BUCKETS
)


class MemPool:
    def __init__(self, coin, daemon, db, state_lock: asyncio.Lock, refresh_secs=1.0, log_status_secs=120.0):
        self.coin = coin
        self._daemon = daemon
        self._db = db
        self._touched_mp = {}
        self._touched_bp = {}
        self._highest_block = -1

        self.logger = class_logger(__name__, self.__class__.__name__)
        self.txs = {}
        self.hashXs = defaultdict(set)  # None can be a key
        self.cached_compact_histogram = []
        self.refresh_secs = refresh_secs
        self.log_status_secs = log_status_secs
        # Prevents mempool refreshes during fee histogram calculation
        self.lock = state_lock
        self.wakeup = asyncio.Event()
        self.mempool_process_time_metric = mempool_process_time_metric
        self.notified_mempool_txs = set()
        self.notify_sessions: Optional[Callable[[int, Set[bytes], Set[bytes]], Awaitable[None]]] = None

    async def _logging(self, synchronized_event):
        """Print regular logs of mempool stats."""
        self.logger.info('beginning processing of daemon mempool.  '
                         'This can take some time...')
        start = time.perf_counter()
        await synchronized_event.wait()
        elapsed = time.perf_counter() - start
        self.logger.info(f'synced in {elapsed:.2f}s')
        while True:
            self.logger.info(f'{len(self.txs):,d} txs '
                             f'touching {len(self.hashXs):,d} addresses')
            await asyncio.sleep(self.log_status_secs)
            await synchronized_event.wait()

    def _accept_transactions(self, tx_map, utxo_map, touched):
        """Accept transactions in tx_map to the mempool if all their inputs
        can be found in the existing mempool or a utxo_map from the
        DB.

        Returns an (unprocessed tx_map, unspent utxo_map) pair.
        """
        hashXs = self.hashXs
        txs = self.txs

        deferred = {}
        unspent = set(utxo_map)
        # Try to find all prevouts so we can accept the TX
        for hash, tx in tx_map.items():
            in_pairs = []
            try:
                for prevout in tx.prevouts:
                    utxo = utxo_map.get(prevout)
                    if not utxo:
                        prev_hash, prev_index = prevout
                        # Raises KeyError if prev_hash is not in txs
                        utxo = txs[prev_hash].out_pairs[prev_index]
                    in_pairs.append(utxo)
            except KeyError:
                deferred[hash] = tx
                continue

            # Spend the prevouts
            unspent.difference_update(tx.prevouts)

            # Save the in_pairs, compute the fee and accept the TX
            tx.in_pairs = tuple(in_pairs)
            # Avoid negative fees if dealing with generation-like transactions
            # because some in_parts would be missing
            tx.fee = max(0, (sum(v for _, v in tx.in_pairs) -
                             sum(v for _, v in tx.out_pairs)))
            txs[hash] = tx

            for hashX, value in itertools.chain(tx.in_pairs, tx.out_pairs):
                touched.add(hashX)
                hashXs[hashX].add(hash)

        return deferred, {prevout: utxo_map[prevout] for prevout in unspent}

    async def _mempool_loop(self, synchronized_event):
        try:
            return await self._refresh_hashes(synchronized_event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.exception("MEMPOOL DIED")
            raise e

    async def _refresh_hashes(self, synchronized_event):
        """Refresh our view of the daemon's mempool."""
        while True:
            start = time.perf_counter()
            height = self._daemon.cached_height()
            hex_hashes = await self._daemon.mempool_hashes()
            if height != await self._daemon.height():
                continue
            hashes = {hex_str_to_hash(hh) for hh in hex_hashes}
            async with self.lock:
                new_hashes = hashes.difference(self.notified_mempool_txs)
                touched = await self._process_mempool(hashes)
                self.notified_mempool_txs.update(new_hashes)
                new_touched = {
                    touched_hashx for touched_hashx, txs in self.hashXs.items() if txs.intersection(new_hashes)
                }
            synchronized_event.set()
            synchronized_event.clear()
            await self.on_mempool(touched, new_touched, height)
            duration = time.perf_counter() - start
            self.mempool_process_time_metric.observe(duration)
            try:
                # we wait up to `refresh_secs` but go early if a broadcast happens (which triggers wakeup event)
                await asyncio.wait_for(self.wakeup.wait(), timeout=self.refresh_secs)
            except asyncio.TimeoutError:
                pass
            finally:
                self.wakeup.clear()

    async def _process_mempool(self, all_hashes):
        # Re-sync with the new set of hashes
        txs = self.txs

        hashXs = self.hashXs  # hashX: [tx_hash, ...]
        touched = set()

        # First handle txs that have disappeared
        for tx_hash in set(txs).difference(all_hashes):
            tx = txs.pop(tx_hash)
            tx_hashXs = {hashX for hashX, value in tx.in_pairs}
            tx_hashXs.update(hashX for hashX, value in tx.out_pairs)
            for hashX in tx_hashXs:
                hashXs[hashX].remove(tx_hash)
                if not hashXs[hashX]:
                    del hashXs[hashX]
            touched.update(tx_hashXs)

        # Process new transactions
        new_hashes = list(all_hashes.difference(txs))
        if new_hashes:
            fetches = []
            for hashes in chunks(new_hashes, 200):
                fetches.append(self._fetch_and_accept(hashes, all_hashes, touched))
            tx_map = {}
            utxo_map = {}
            for fetch in asyncio.as_completed(fetches):
                deferred, unspent = await fetch
                tx_map.update(deferred)
                utxo_map.update(unspent)

            prior_count = 0
            # FIXME: this is not particularly efficient
            while tx_map and len(tx_map) != prior_count:
                prior_count = len(tx_map)
                tx_map, utxo_map = self._accept_transactions(tx_map, utxo_map, touched)

            if tx_map:
                self.logger.info(f'{len(tx_map)} txs dropped')

        return touched

    async def _fetch_and_accept(self, hashes, all_hashes, touched):
        """Fetch a list of mempool transactions."""
        raw_txs = await self._daemon.getrawtransactions((hash_to_hex_str(hash) for hash in hashes))

        to_hashX = self.coin.hashX_from_script
        deserializer = self.coin.DESERIALIZER

        tx_map = {}
        for hash, raw_tx in zip(hashes, raw_txs):
            # The daemon may have evicted the tx from its
            # mempool or it may have gotten in a block
            if not raw_tx:
                continue
            tx, tx_size = deserializer(raw_tx).read_tx_and_vsize()
            # Convert the inputs and outputs into (hashX, value) pairs
            # Drop generation-like inputs from MemPoolTx.prevouts
            txin_pairs = tuple((txin.prev_hash, txin.prev_idx)
                               for txin in tx.inputs
                               if not txin.is_generation())
            txout_pairs = tuple((to_hashX(txout.pk_script), txout.value)
                                for txout in tx.outputs)
            tx_map[hash] = MemPoolTx(txin_pairs, None, txout_pairs,
                                     0, tx_size, raw_tx)

        # Determine all prevouts not in the mempool, and fetch the
        # UTXO information from the database.  Failed prevout lookups
        # return None - concurrent database updates happen - which is
        # relied upon by _accept_transactions. Ignore prevouts that are
        # generation-like.
        prevouts = tuple(prevout for tx in tx_map.values()
                         for prevout in tx.prevouts
                         if prevout[0] not in all_hashes)
        utxos = await self._db.lookup_utxos(prevouts)
        utxo_map = dict(zip(prevouts, utxos))

        return self._accept_transactions(tx_map, utxo_map, touched)

    #
    # External interface
    #

    async def keep_synchronized(self, synchronized_event):
        """Keep the mempool synchronized with the daemon."""
        await asyncio.wait([
            self._mempool_loop(synchronized_event),
            # self._refresh_histogram(synchronized_event),
            self._logging(synchronized_event)
        ])

    async def balance_delta(self, hashX):
        """Return the unconfirmed amount in the mempool for hashX.

        Can be positive or negative.
        """
        value = 0
        if hashX in self.hashXs:
            for hash in self.hashXs[hashX]:
                tx = self.txs[hash]
                value -= sum(v for h168, v in tx.in_pairs if h168 == hashX)
                value += sum(v for h168, v in tx.out_pairs if h168 == hashX)
        return value

    def compact_fee_histogram(self):
        """Return a compact fee histogram of the current mempool."""
        return self.cached_compact_histogram

    async def potential_spends(self, hashX):
        """Return a set of (prev_hash, prev_idx) pairs from mempool
        transactions that touch hashX.

        None, some or all of these may be spends of the hashX, but all
        actual spends of it (in the DB or mempool) will be included.
        """
        result = set()
        for tx_hash in self.hashXs.get(hashX, ()):
            tx = self.txs[tx_hash]
            result.update(tx.prevouts)
        return result

    def transaction_summaries(self, hashX):
        """Return a list of MemPoolTxSummary objects for the hashX."""
        result = []
        for tx_hash in self.hashXs.get(hashX, ()):
            tx = self.txs[tx_hash]
            has_ui = any(hash in self.txs for hash, idx in tx.prevouts)
            result.append(MemPoolTxSummary(tx_hash, tx.fee, has_ui))
        return result

    async def unordered_UTXOs(self, hashX):
        """Return an unordered list of UTXO named tuples from mempool
        transactions that pay to hashX.

        This does not consider if any other mempool transactions spend
        the outputs.
        """
        utxos = []
        for tx_hash in self.hashXs.get(hashX, ()):
            tx = self.txs.get(tx_hash)
            for pos, (hX, value) in enumerate(tx.out_pairs):
                if hX == hashX:
                    utxos.append(UTXO(-1, pos, tx_hash, 0, value))
        return utxos

    def get_mempool_height(self, tx_hash):
        # Height Progression
        #   -2: not broadcast
        #   -1: in mempool but has unconfirmed inputs
        #    0: in mempool and all inputs confirmed
        # +num: confirmed in a specific block (height)
        if tx_hash not in self.txs:
            return -2
        tx = self.txs[tx_hash]
        unspent_inputs = sum(1 if hash in self.txs else 0 for hash, idx in tx.prevouts)
        if unspent_inputs:
            return -1
        return 0

    async def _maybe_notify(self, new_touched):
        tmp, tbp = self._touched_mp, self._touched_bp
        common = set(tmp).intersection(tbp)
        if common:
            height = max(common)
        elif tmp and max(tmp) == self._highest_block:
            height = self._highest_block
        else:
            # Either we are processing a block and waiting for it to
            # come in, or we have not yet had a mempool update for the
            # new block height
            return
        touched = tmp.pop(height)
        for old in [h for h in tmp if h <= height]:
            del tmp[old]
        for old in [h for h in tbp if h <= height]:
            touched.update(tbp.pop(old))
        # print("notify", height, len(touched), len(new_touched))
        await self.notify_sessions(height, touched, new_touched)

    async def start(self, height, session_manager: 'LBRYSessionManager'):
        self._highest_block = height
        self.notify_sessions = session_manager._notify_sessions
        await self.notify_sessions(height, set(), set())

    async def on_mempool(self, touched, new_touched, height):
        self._touched_mp[height] = touched
        await self._maybe_notify(new_touched)

    async def on_block(self, touched, height):
        self._touched_bp[height] = touched
        self._highest_block = height
        await self._maybe_notify(set())
