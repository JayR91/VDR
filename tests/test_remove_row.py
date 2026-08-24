"""Integration test: real Tk + real App + real QueueManager.

Drives the actual remove_selected() path against a real ttk.Treeview and the
real event queue, so it exercises the resurrection bug end to end.
"""
import os, sys, time
sys.path.insert(0, os.path.expanduser("~/Downloads/VDR2"))
import tkinter as tk
from queue_manager import QueueManager
from engine import Status
import gui as gui_mod

root = tk.Tk()
root.withdraw()                      # don't steal focus
qm = QueueManager(max_concurrent=1)
app = gui_mod.App(root, qm)
root.withdraw()

# A task that will fail fast; we only care about row bookkeeping.
task = qm.add("https://example.invalid/never.zip", "/tmp/never.zip")
app._drain_events()                  # create the row

rows = app.tree.get_children()
assert len(rows) == 1, f"expected 1 row, got {rows}"
print(f"row created                    -> rows={len(rows)}")

# Select it exactly as a mouse click would.
app.tree.selection_set(rows[0])
assert app._selected_task() is task, "selection did not resolve to the task"
print("selection resolves to task     -> ok")

app.remove_selected()
rows = app.tree.get_children()
assert len(rows) == 0, f"row should be gone immediately, got {rows}"
print(f"after remove_selected          -> rows={len(rows)}")

# The cancel triggered by remove queues a status update. Drain it repeatedly,
# which is what the running app does every 200ms.
for _ in range(5):
    app._drain_events()
    root.update()
    time.sleep(0.05)

rows = app.tree.get_children()
print(f"after draining cancel events   -> rows={len(rows)}")
assert len(rows) == 0, "REGRESSION: the removed row was resurrected"

assert task not in app.row_by_task, "task still tracked in row_by_task"
print("\nPASS - remove is permanent")
root.destroy()
