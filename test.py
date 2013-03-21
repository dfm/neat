#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import (division, print_function, absolute_import,
                        unicode_literals)
import time
from gmotr.gmail_imap import GmailIMAPAccount


if __name__ == "__main__":
    gm = GmailIMAPAccount("foreman.mackey@gmail.com")
    strt = time.time()
    gm.list_remote()
    print(time.time() - strt)
