"""Stands in for chrome.exe in the lifecycle tests: takes Chrome's arguments
(so its command line carries --user-data-dir like the real thing) and sleeps
until it is stopped."""

import time

while True:
    time.sleep(1)
