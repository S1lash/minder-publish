"""Concurrent profile access waits briefly without mixing credentials."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from minder_publish.client import config
from minder_publish.client.errors import ClientError


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv('MINDER_PUBLISH_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    for key in ('MINDER_PUBLISH_URL', 'MINDER_PUBLISH_TOKEN', 'MINDER_PUBLISH_TOKEN_FILE'):
        monkeypatch.delenv(key, raising=False)
    config.save_instance('fixture', 'https://one.invalid', 'fixture-one')


def test_reader_waits_until_writer_releases_and_reads_consistent_pair(profile, monkeypatch):
    contended = threading.Event()
    wait = config._wait_for_configuration_lock

    def observe_wait(acquire):
        def observe_acquire():
            try:
                acquire()
            except OSError:
                contended.set()
                raise

        wait(observe_acquire)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with config.configuration_lock():
            monkeypatch.setattr(config, '_wait_for_configuration_lock', observe_wait)
            future = pool.submit(config.connection, 'fixture', use_environment=False)
            assert contended.wait(2)
            assert not future.done()
        selected = future.result(timeout=3)
    assert (selected.base_url, selected.token) == ('https://one.invalid', 'fixture-one')


def test_many_parallel_reads_succeed(profile):
    barrier = threading.Barrier(12)
    def read(_):
        barrier.wait()
        value = config.connection('fixture', use_environment=False)
        return value.base_url, value.token
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(read, range(12)))
    assert results == [('https://one.invalid', 'fixture-one')] * 12


def test_lock_timeout_is_bounded_and_fails_closed(profile, monkeypatch):
    monkeypatch.setattr(config, 'LOCK_TIMEOUT_SECONDS', .12)
    start = time.monotonic()
    with config.configuration_lock():
        with pytest.raises(ClientError) as error:
            config.connection('fixture', use_environment=False)
    elapsed = time.monotonic() - start
    assert error.value.code == 'configuration_busy'
    assert .10 <= elapsed < 1.5


def test_non_contention_lock_error_is_not_retried(monkeypatch):
    import errno
    calls = []
    def fail():
        calls.append(1)
        raise OSError(errno.EIO, 'synthetic I/O error')
    with pytest.raises(OSError) as error:
        config._wait_for_configuration_lock(fail)
    assert error.value.errno == errno.EIO and len(calls) == 1
