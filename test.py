#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import (division, print_function, absolute_import,
                        unicode_literals)
import time
from gmotr.gmail_imap import GmailIMAPAccount
import gmotr.models as models


if __name__ == "__main__":
    models.drop_all()
    models.create_all()
    gm = GmailIMAPAccount("foreman.mackey@gmail.com")
    strt = time.time()
    gm.list_remote()
    print(time.time() - strt)
