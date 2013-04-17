#!/usr/bin/env python

__all__ = []

import re
import time
from datetime import datetime
import socket
import imaplib
from email.parser import Parser
from email.header import decode_header
import getpass
import logging

import keyring

from . import models


list_re = re.compile(r"\((?P<flags>.*?)\) \"(?P<delimiter>.*)\" (?P<name>.*)")


def parse_mbname(txt):
    flags, delim, mbname = list_re.match(txt).groups()
    return flags.split(), mbname.strip().strip("\"")


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

        session = models.Session()
        self.account = session.query(models.GmailAccount).filter(
            models.GmailAccount.email == self.email).first()
        session.close()

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

    def list_remote(self, mb="[Gmail]/All Mail", q=""):
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

            # Limit to the N most recent messages.
            if len(uids) > 100:
                uids = uids[-100:]

            # Get the information about the messages.
            code, data = c.uid("fetch", ",".join(uids),
                               "(BODY.PEEK[HEADER.FIELDS "
                               "(Subject From To Cc Bcc Date)] "
                               "X-GM-MSGID X-GM-THRID X-GM-LABELS "
                               "FLAGS INTERNALDATE)")
            if code != "OK":
                raise IMAPSyncError("Couldn't fetch info for messages ({})"
                                    .format(code))

            # Parse the returned data and submit them to the DB session.
            session = models.Session()
            parser = Parser()
            for m in data[::2]:
                headers = parser.parsestr(m[1].decode("utf-8"))
                parameters = parse_imap_parameters(m[0].decode("utf-8"))

                # Resolve duplicate email addresses.
                from_email = _decode(headers.get("From"))
                if "To" in headers:
                    to_emails = [_decode(h) for h in headers.get_all("To")]
                else:
                    to_emails = []
                if "Cc" in headers:
                    cc_emails = [_decode(h) for h in headers.get_all("Cc")]
                else:
                    cc_emails = []
                if "Bcc" in headers:
                    bcc_emails = [_decode(h) for h in headers.get_all("Bcc")]
                else:
                    bcc_emails = []
                emails = [_decode(e) for e in [from_email] + to_emails
                          + cc_emails + bcc_emails]

                emails = dict(session.query(models.GmailAddress.raw,
                                            models.GmailAddress)
                              .filter(models.GmailAddress.raw.in_(emails)))

                from_email = emails.get(from_email,
                                        models.GmailAddress(from_email))
                to_emails = [emails.get(e, models.GmailAddress(e))
                             for e in to_emails]
                cc_emails = [emails.get(e, models.GmailAddress(e))
                             for e in cc_emails]
                bcc_emails = [emails.get(e, models.GmailAddress(e))
                              for e in bcc_emails]

                # Resolve duplicates in flags and labels.
                flags = parameters.get("FLAGS", [])
                if len(flags) > 0:
                    all_flags = dict(session.query(models.GmailFlag.name,
                                                   models.GmailFlag)
                                     .filter(models.GmailFlag.name.in_(flags)))
                    flags = [all_flags.get(f, models.GmailFlag(f))
                             for f in flags]

                labels = parameters.get("X-GM-LABELS", [])
                if len(labels) > 0:
                    all_labels = dict(session.query(models.GmailLabel.name,
                                                    models.GmailLabel)
                                      .filter(models.GmailLabel.name.in_(
                                          labels)))
                    labels = [all_labels.get(l, models.GmailLabel(l))
                              for l in labels]

                msg = models.GmailMessage(parameters["UID"],
                                          parameters["X-GM-MSGID"],
                                          parameters["X-GM-THRID"],
                                          self.account,
                                          parameters["INTERNALDATE"],
                                          _decode(headers.get("Date")),
                                          _decode(headers.get("Subject")),
                                          from_email,
                                          to_emails,
                                          cc_emails,
                                          bcc_emails,
                                          flags,
                                          labels)

                session.add(msg)

            session.commit()


_imap_header_re = re.compile(r"(?:([A-Z\-]+?) \((.*?)\))"
                             r"|(?:([A-Z\-]+?) \"(.*?)\")"
                             r"|(?:([A-Z\-]+?) (\S+?)\b)")
_imap_header_list_re = re.compile(r"((?:\"(?:.+?)\")|(?:(?:\S+)))")


def parse_imap_parameters(hdr):
    # Skip the prefix.
    ind = hdr.index("(")

    # Find all the parameter groups.
    groups = _imap_header_re.findall(hdr[ind + 1:])

    # Parse through the list to find the non-empty results.
    results = {}
    for r in groups:
        if r[0] != "":
            # Lists are surrounded in parens, split the list here.
            li = _imap_header_list_re.findall(r[1])
            results[r[0]] = [l.strip("\"") for l in li]
        elif r[2] != "":
            results[r[2]] = r[3]
        elif r[4] != "":
            results[r[4]] = r[5]

    # Parse the INTERNALDATE parameter to a ``datetime`` object.
    if "INTERNALDATE" in results:
        dt = "INTERNALDATE \"{}\"".format(results.pop("INTERNALDATE"))
        tpl = imaplib.Internaldate2tuple(dt.encode("utf-8"))
        if tpl is None:
            results["INTERNALDATE"] = None
        else:
            results["INTERNALDATE"] = datetime.fromtimestamp(time.mktime(tpl))

    return results


def _decode(h):
    t, enc = decode_header(str(h))[0]
    if enc is not None:
        t = t.decode(enc)
    return t


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
