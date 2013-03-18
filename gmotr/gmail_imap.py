#!/usr/bin/env python

__all__ = []


import re
import socket
import imaplib
from email.parser import Parser
import getpass
import logging

import keyring


class GmailIMAPAccount(object):

    def __init__(self, email):
        self.email = email
        self.password = None
        self.imap = IMAPConnection(email, self.get_password(),
                                   "imap.gmail.com", 993)

        # Check the authentication credentials.
        logging.info("Checking IMAP authentication credentials...")

        try:
            # Try to open an IMAP connection.
            with self.imap as c:
                c

        except imaplib.IMAP4.error as e:
            # Determine the reason for failure.
            msg = str(e)
            t = re.findall("\[(.*?)\]", msg)
            if len(t) == 0:
                raise IMAPAuthError()

            logging.error("IMAP connection failed with the error: {}"
                          .format(t[0]))
            raise IMAPAuthError(t[0])

        except socket.error as e:
            logging.error("The network connection failed.")
            raise e

    def get_password(self, force=False):
        """
        Retrieve the password associated with this account from the keychain
        or ask the user for one.

        """
        pw = None

        # Try to retrieve the password from the system keychain.
        if not force:
            if self.password is not None:
                return self.password
            pw = keyring.get_password("gmotr", self.email)

        # If that didn't succeed (or if ``force is True``).
        if pw is None:
            pw = getpass.getpass("Password for {}: ".format(self.email))
            f = raw_input("Save password in key chain? [Y/n] ")
            if f in ["Y", "y", ""]:
                keyring.set_password("gmotr", self.email, pw)

        # Cache the result.
        self.password = pw
        return self.password

    def list_remote(self, mb="[Gmail]/All Mail", q="in:inbox"):
        with self.imap as c:
            # Select the requested mailbox.
            code, count = c.select(mailbox="\"{}\"".format(mb), readonly=True)
            if code != "OK":
                raise IMAPSyncError("Couldn't SELECT '{}' ({})"
                                    .format(mb, code))

            # Run the Gmail style search query.
            code, uids = c.uid("search", None, "X-GM-RAW", "\"{}\"".format(q))
            if code != "OK" or len(uids) < 1:
                raise IMAPSyncError("Couldn't run query '{}' ({})"
                                    .format(q, code))

            # Parse the returned UIDs.
            uids = uids[0].decode("utf-8").split()
            if len(uids) == 0:
                return []

            # Get the information about the messages.
            code, data = c.uid("fetch", ",".join(uids),
                               "(BODY.PEEK[HEADER] "
                               # "(Subject From To Date)] "
                               "X-GM-MSGID X-GM-THRID X-GM-LABELS "
                               "FLAGS INTERNALDATE)")
            if code != "OK":
                raise IMAPSyncError("Couldn't fetch info for messages ({})"
                                    .format(code))

            # Parse the returned data.
            results = []
            parser = Parser()
            for m in data[::2]:
                headers = parser.parsestr(m[1].decode("utf-8"))
                print(parse_imap_header(m[0].decode("utf-8")))


def parse_imap_header(hdr):
    ind = hdr.index("(")
    print(hdr[ind + 1:])
    results = re.findall(r"(?:(\S+?) \((.+?)\))"
                         r"|(?:(\S+?) \"(.+?)\")"
                         r"|(?:(\S+?) (\S+?)\b)", hdr[ind + 1:])
    print(results)
    return None


class IMAPConnection(object):
    """
    A wrapper around an :class:`imaplib.IMAP4_SSL` object that allows it to
    be used in a ``with`` block.

    """
    def __init__(self, email, password, server, port):
        self.email = email
        self.password = password
        self.server = server
        self.port = port

    def __enter__(self):
        self.connection = imaplib.IMAP4_SSL(self.server, self.port)
        self.connection.login(self.email, self.password)
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        self.connection.logout()


class IMAPAuthError(Exception):
    pass


class IMAPSyncError(Exception):
    pass


if __name__ == "__main__":
    gm = GmailIMAPAccount("foreman.mackey@gmail.com")
    gm.list_remote()
