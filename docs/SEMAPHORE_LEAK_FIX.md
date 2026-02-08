# Multiprocessing Semaphore Leak - Analysis & Fix

## The Issue

When closing the score-app application, Python 3.13 occasionally warns about leaked semaphore objects:

```
/Library/Frameworks/Python.framework/Versions/3.13/lib/python3.13/multiprocessing/resource_tracker.py:324: UserWarning:
resource_tracker: There appear to be 3 leaked semaphore objects to clean up at shutdown:
{'/mp-wl8fhjv6', '/mp-1tjpvpdu', '/mp-67tjwtqc'}
```

## Root Cause

The score-app uses `multiprocessing.Queue` for log message passing between the pusher process and the main process.

A `multiprocessing.Queue` internally creates **3 semaphore objects**:
1. **`_sem`** - Counting semaphore for queue capacity management
2. **`_rlock`** - Reentrant lock for thread-safe queue operations
3. **`_wlock`** - Write lock for the internal feeder thread

### The Bug

The original shutdown code in `app.py` was:

```python
# Stop the queue listener (this drains remaining items)
queue_listener.stop()

# Cancel the join thread to avoid blocking, then close the queue
log_queue.cancel_join_thread()  # ❌ PROBLEM
log_queue.close()
```

**Why this causes leaks:**

1. `cancel_join_thread()` tells the queue: "don't wait for the internal feeder thread to finish"
2. The feeder thread may still be holding references to the 3 semaphores
3. When `close()` is called immediately after, the semaphores aren't properly released
4. Python 3.13's stricter resource tracker detects this and issues a warning

### Why "Sometimes"?

The leak is **timing-dependent**:
- **No leak**: If the pusher process stops cleanly and the queue drains fully before shutdown
- **Leak**: If the pusher is force-terminated or the queue has pending items when closing

## The Fix

### Changes Made

1. **Proper queue thread joining** (app.py:909-918):
   ```python
   # Close queue first
   log_queue.close()

   # Wait for feeder thread to finish naturally
   try:
       log_queue.join_thread()  # ✓ Proper cleanup
   except Exception as e:
       logger.warning(f"Queue join_thread failed: {e}, canceling...")
       log_queue.cancel_join_thread()
   ```

2. **Child process cleanup** (app.py:952-955):
   ```python
   finally:
       # Clean up logging to ensure queue is flushed
       root_logger.removeHandler(queue_handler)
       queue_handler.close()
   ```

3. **Added flush delay** (app.py:902-904):
   ```python
   # Give the queue handler in the child process time to flush
   # and the queue listener a moment to receive final messages
   time.sleep(0.3)
   ```

### Complete Shutdown Sequence

```python
1. uvicorn.run() exits (user pressed Ctrl+C)
2. pusher_process.join(timeout=5) - wait for pusher to stop
3. If still alive: terminate() or kill()
4. time.sleep(0.3) - give queue time to flush remaining messages
5. queue_listener.stop() - drain queue and stop listener
6. log_queue.close() - mark queue as closed
7. log_queue.join_thread() - wait for feeder thread to finish (releases semaphores)
8. Done - no leaks!
```

## Testing

Created `/tmp/test_mp_cleanup.py` to verify the fix:
- Spawns a worker process that logs to a multiprocessing.Queue
- Uses proper cleanup sequence: close() → join_thread()
- Verifies no resource_tracker warnings are issued

**Result**: ✓ Test PASSED - No semaphore leaks!

## Key Insights

1. **Never use `cancel_join_thread()`** unless you have a very good reason
   - It exists for edge cases where you can't wait
   - Almost always, you should use `join_thread()` instead

2. **Proper queue cleanup order**:
   ```python
   queue.close()        # Stop accepting new items
   queue.join_thread()  # Wait for feeder thread (releases semaphores)
   ```

3. **Python 3.13 changes**:
   - Previous Python versions may have leaked too, but didn't warn
   - Python 3.13 has stricter resource tracking

4. **Timing matters**:
   - Ensure child processes finish writing before closing queues
   - Use `time.sleep()` or other synchronization if needed

## References

- Python multiprocessing.Queue source: https://github.com/python/cpython/blob/main/Lib/multiprocessing/queues.py
- Resource tracker: https://github.com/python/cpython/blob/main/Lib/multiprocessing/resource_tracker.py
- PEP 3151: Reworking the OS and IO exception hierarchy
