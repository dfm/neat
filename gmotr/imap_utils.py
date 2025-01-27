# vim: set fileencoding=utf-8 :

from __future__ import print_function

__all__ = ["GmailAccount"]

import os
import re
import sys
import time
import email
from email.generator import Generator
import imaplib
import sqlite3
import logging
import StringIO
import getpass
import curses
import socket

import keyring


# UI stuff.
curses.setupterm()
_ui_cll = curses.tigetstr(u"cuu1") \
        + curses.tigetstr(u"cr") \
        + curses.tigetstr(u"el")


# Response parsing.

class IMAPSyncError(Exception):
    pass


class GmailAccount(object):
    """
    An Gmail account that is to be synced locally.

    :param email_address:
        The Gmail email address for the account.

    :param maildir: (optional)
        The path to the Maildir local repository. It will be created if
        it doesn't already exist.

    """

    parse_uid = re.compile(r"UID ([0-9]*)")
    parse_x_gm_msgid = re.compile(r"X-GM-MSGID ([0-9]*)")
    parse_x_gm_thrid = re.compile(r"X-GM-THRID ([0-9]*)")
    parse_x_gm_labels = re.compile(r"X-GM-LABELS \((.*?)\)")
    parse_flags = re.compile(r"FLAGS \((.*?)\)")
    parse_internaldate = re.compile(r"INTERNALDATE \"(.*?)\"")
    parse_header = re.compile(r"(.*?)\: (?P<from_>.*?)$", re.M | re.S)

    parse_labels = re.compile(r"")
    parse_labels = re.compile(r"(\"(?:.*?)\"|(?:.+?(?:\s|\Z)))")

    def __init__(self, email_address):
        self._email = email_address
        self._password = None
        self._imap = IMAPConnection(self._email, self.get_password())

        success = False
        while not success:
            # Check the login credentials.
            print(u"Checking IMAP credentials...", end=" ")
            try:
                with self._imap as c:
                    c

            except imaplib.IMAP4.error as e:
                # Probably an authentication error.
                msg = str(e)
                t = re.findall(u"\[(.*?)\]", msg)

                if len(t) == 0:
                    raise

                if t[0] == u"AUTHENTICATIONFAILED":
                    print(u"Failed: authentication error.\n\n"
                        u"Your credentials seem to be wrong. "
                        u"Make sure that IMAP is enabled for your Gmail "
                        u"account and try again.\n")

                    self._imap = IMAPConnection(self._email,
                                                self.get_password(force=True))

                else:
                    raise

            except socket.error as e:
                # Network problems.
                print(u"Failed: network error.\n\n"
                    u"Make sure that you're connected to the internet and try "
                    u"again.")
                sys.exit(1)

            else:
                print(u"Success.\n")
                success = True

    def sync_setup(self, maildir=None):
        # Initialize the Maildir.
        if maildir is None:
            maildir = os.path.expanduser(os.path.join(u"~",
                                                      u".gmotr",
                                                      u"mail",
                                                      self._email))
        self._maildir = maildir

        # Initialize the database back end.
        self._db = IMAPDB(os.path.join(maildir, "imapsync.db"))

        # Set up the tables.
        with self._db as c:
            c.execute(u"""CREATE TABLE IF NOT EXISTS messages
                (gm_msgid INTEGER PRIMARY KEY,
                 gm_thrid INTEGER,
                 uid INTEGER UNIQUE,
                 mailbox TEXT,
                 answered INTEGER,
                 flagged INTEGER,
                 draft INTEGER,
                 deleted INTEGER,
                 seen INTEGER,
                 subject TEXT,
                 mail_from TEXT,
                 mail_to TEXT)""")

            c.execute(u"""CREATE TABLE IF NOT EXISTS labels
                (id INTEGER PRIMARY KEY,
                 gm_msgid INTEGER,
                 label TEXT)""")

            c.execute(u"""CREATE VIRTUAL TABLE IF NOT EXISTS contents
                 USING FTS3(mail_from, mail_to, cc, bcc, subject, body)""")

    def get_password(self, force=False):
        pw = None

        if not force:
            if self._password is not None:
                return self._password
            pw = keyring.get_password(u"gmotr", self._email)

        if pw is None:
            pw = getpass.getpass(u"Password for {0}: ".format(self._email))
            f = raw_input(u"Save password in key chain? [Y/n] ")
            if f in [u"Y", u"y", u""]:
                keyring.set_password(u"gmotr", self._email, pw)

        return pw

    def simple_list(self, q=None):
        with self._imap as c:
            if q is None:
                q = u"in:inbox"

            code, count = c.select("[Gmail]/All Mail", readonly=True)
            if code != u"OK":
                raise IMAPSyncError(u"Couldn't SELECT 'All Mail' ({1})"
                                    .format(code))

            code, uids = c.uid(u"search", None,
                                u"X-GM-RAW", u"\"{0}\"".format(q))

            if code != u"OK":
                raise IMAPSyncError(u"Couldn't run query '{0}' ({1})"
                                    .format(q, code))

            uids = u",".join(uids[0].split())

            code, data = c.uid(u"fetch", uids,
                               u"(BODY.PEEK[HEADER.FIELDS "
                               u"(Subject From To Date)] "
                               u"X-GM-MSGID X-GM-THRID X-GM-LABELS "
                               u"FLAGS INTERNALDATE)")

            if code != "OK":
                raise IMAPSyncError(u"Couldn't FETCH flags.")

            results = []
            for d in data:
                if d != u")":
                    doc = dict(zip(
                            [u"msgid", u"thrid", u"labels", u"flags", u"uid"],
                            self._do_header_parse(d[0])))
                    for k, val in self.parse_header.findall(d[1]):
                        doc[k.strip().lower()] = val.strip()

                    doc["time"] = time.mktime(imaplib.Internaldate2tuple(d[0]))

                    results.append(doc)

            return sorted(results, reverse=True, key=lambda d: d["time"])

    def fetch_message(self, uid):
        with self._imap as c:
            # Select the IMAP mailbox.
            code, count = c.select("[Gmail]/All Mail", readonly=True)
            if code != u"OK":
                raise IMAPSyncError(u"Couldn't SELECT 'All Mail' ({1})"
                                    .format(code))

            code, data = c.uid(u"fetch", unicode(uid),
                               u"(BODY.PEEK[] "
                               u"X-GM-MSGID X-GM-THRID X-GM-LABELS "
                               u"FLAGS INTERNALDATE)")

            if code != "OK":
                raise IMAPSyncError(u"Couldn't FETCH body.")

            doc = dict(zip([u"msgid", u"thrid", u"labels", u"flags", u"uid"],
                           self._do_header_parse(data[0][0])))
            for k, val in self.parse_header.findall(data[0][0]):
                doc[k.strip().lower()] = val.strip()

            doc["time"] = time.mktime(imaplib.Internaldate2tuple(data[0][0]))
            doc["message"] = email.message_from_string(data[0][1])

            for k in ["from", "to", "subject"]:
                doc[k] = ",".join(doc["message"].get_all(k))

            return doc

    def _fetch(self, mb, mbname):
        """
        Synchronize the local repository with the remote one. NOTE: this is a
        *read-only* operation.

        :param mb:
            The name of the mailbox in remote repository (e.g.
            ``'[Gmail]/All Mail'``).

        :param mbname:
            A file-system friendly name for the mailbox (e.g. ``archive``).

        """
        # Determine the last_ UID that was fetched for this mailbox.
        with self._db as c:
            last_uid = c.execute(u"""SELECT max(uid) FROM messages
                        WHERE mailbox=?""", (mb, )) \
                        .fetchone()[0]

        if last_uid is None:
            last_uid = 0

        # Set up the Maildir.
        maildir = CustomMaildir(os.path.join(self._maildir, mbname))

        with self._imap as c:
            # Select the IMAP mailbox.
            code, count = c.select(mb, readonly=True)
            if code != u"OK":
                raise IMAPSyncError(u"Couldn't SELECT '{0}' ({1})"
                                    .format(mb, code))
            logging.info(u"Found {0} messages in {1}."
                         .format(int(count[0]), mb))

            if last_uid > 0:
                # Update the flags and labels for the existing messages.
                code, data = c.uid(u"fetch",
                                   u"1:{0}".format(last_uid),
                                   u"(X-GM-MSGID X-GM-THRID X-GM-LABELS "
                                   u"FLAGS)")

                if code != "OK":
                    raise IMAPSyncError(u"Couldn't FETCH flags.")

                self._parse_flags_response(data)

            # Get all the messages with ``UID > last_uid``.
            code, data = c.uid(u"search",
                               None,
                               u"UID {0}:*".format(last_uid + 1))
            if code != u"OK":
                raise IMAPSyncError(u"Couldn't SEARCH in '{0}' ({1})"
                                    .format(mb, code))
            uids = [int(uid) for uid in data[0].split()]

            # Loop over the ``uid``s and fetch the messages.
            ntot, nbars = len(uids), 50
            for n, uid in enumerate(uids):
                # Show progress.
                if n > 0:
                    sys.stdout.write(2 * _ui_cll)
                else:
                    strt = time.time()

                # How many bars filled on the progress bar?
                nfilled = int(float(n) / ntot * nbars)

                # Show the summary comment.
                sys.stdout.write(u"Fetching message {0} of {1} "
                                 .format(n + 1, ntot)
                                 + u"in mailbox: '{0}'\n".format(mb))

                # Show the progress bar.
                sys.stdout.write(u"| " + u"=" * nfilled
                                 + u"-" * (nbars - nfilled) + u" | ")

                # Compute and display the approximate time remaining.
                if n > 0:
                    t = (time.time() - strt) / n * ntot
                    h = int(t / 3600.)
                    m = int((t - h * 3600.) / 60.)
                    s = int(t - h * 3600. - m * 60.)
                    sys.stdout.write(u"approx. {0:02d}:{1:02d}:{2:02d} left"
                                     .format(h, m, s))

                sys.stdout.write(u"\n")
                sys.stdout.flush()

                # Fetch the message.
                code, data = c.uid(u"fetch",
                                   unicode(uid),
                                   u"(X-GM-MSGID X-GM-THRID X-GM-LABELS "
                                   u"FLAGS BODY.PEEK[])")

                if code != "OK":
                    raise IMAPSyncError(u"Couldn't FETCH {0} in '{1}' ({2})"
                                        .format(uid, mb, code))

                # Parse the response.
                msg = self._parse_msg_response(mb, data)

                # Save the message to a file and update the database.
                with self._db as dbc:
                    msg.save(maildir, dbc)

                logging.info(u"Saved message: {0}".format(msg.msgid))

    def _do_header_parse(self, r):
        # Run regular expressions to parse the response header.
        matches = [p.search(r) for p in [self.parse_uid,
                                         self.parse_x_gm_msgid,
                                         self.parse_x_gm_thrid,
                                         self.parse_x_gm_labels,
                                         self.parse_flags]]

        # Ensure that the regular expressions were all successful.
        if any([m is None for m in matches]):
            raise IMAPSyncError(u"Couldn't parse '{0}'".format(r))

        # Reformat the results.
        uid, msgid, thrid, labels, flags = [m.groups()[0] for m in matches]
        uid = int(uid)
        msgid = int(msgid)
        thrid = int(thrid)
        flags = flags.split()
        labels = [l.strip() for l in self.parse_labels.findall(labels)]

        return msgid, thrid, labels, flags, uid

    def _parse_msg_response(self, mailbox, msg):
        msgid, thrid, labels, flags, uid = self._do_header_parse(msg[0][0])
        return GmailMessage(uid, msgid, thrid, mailbox, flags, labels,
                            msg[0][1])

    def _parse_flags_response(self, resp):
        parsed = [self._do_header_parse(r) for r in resp]
        msgid, thrid, labels, flags, uid = zip(*parsed)

        # Update the database.
        with self._db as dbc:
            # Update the flags.
            dbc.executemany(u"""UPDATE messages SET
                    answered=?, flagged=?, draft=?, deleted=?, seen=?
                    WHERE uid=?
                    """,
                    [(u"\\Answered" in f,
                      u"\\Flagged" in f,
                      u"\\Draft" in f,
                      u"\\Deleted" in f,
                      u"\\Seen" in f,
                      _id) for f, _id in zip(flags, uid)])

            # Update the labels.
            dbc.executemany(u"""INSERT OR REPLACE INTO labels
                    (id, gm_msgid, label)
                    VALUES (
                        (SELECT id FROM labels WHERE gm_msgid=? AND label=?),
                        ?, ?
                    )""",
                    [(msgid[i], l, msgid[i], l) for i in range(len(msgid))
                                                for l in labels[i]])

    def fetch_all(self):
        folders = [("[Gmail]/All Mail", u"archive"),
                   ("[Gmail]/Sent Mail", u"sent"),
                   ("[Gmail]/Drafts", u"drafts")]
        for folder in folders:
            self._fetch(*folder)


class GmailMessage(object):

    def __init__(self, uid, msgid, thrid, mailbox, flags, labels, msg):
        self.uid, self.msgid, self.thrid, self.mailbox = (
                                                uid, msgid, thrid, mailbox)
        self.flags, self.labels = flags, labels

        # Parse the email header/body.
        self.msg = email.message_from_string(msg)

        # Extract and format the relevant fields.
        self.subject = " ".join(self.msg.get_all(u"subject", u""))
        self.sender = self.msg.get(u"from")
        self.to = u", ".join(self.msg.get_all(u"to", u""))
        self.cc = u", ".join(self.msg.get_all(u"cc", u""))
        self.bcc = u", ".join(self.msg.get_all(u"bcc", u""))

        self.body = "".join([p.get_payload(decode=True)
                                for p in self.msg.walk()
                                if p.get_content_type() == u"text/plain"])

    def flatten(self):
        fp = StringIO.StringIO()
        g = Generator(fp, mangle_from_=False, maxheaderlen=60)
        g.flatten(self.msg)
        return fp.getvalue()

    def save(self, maildir, dbc):
        # Save the message to the Maildir.
        maildir.add(self.msgid, self.flatten(), flags=self.flags)

        # Commit the changes to the database.
        dbc.execute(u"""INSERT INTO messages
                    (gm_msgid, gm_thrid, uid, mailbox, answered, flagged,
                     draft, deleted, seen, subject, mail_from, mail_to)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (self.msgid, self.thrid, self.uid, self.mailbox,
                     u"\\Answered" in self.flags,
                     u"\\Flagged" in self.flags,
                     u"\\Draft" in self.flags,
                     u"\\Deleted" in self.flags,
                     u"\\Seen" in self.flags,
                     self.subject, self.sender, self.to))

        dbc.executemany(u"INSERT INTO labels (gm_msgid, label) VALUES (?,?)",
                        [(self.msgid, l) for l in self.labels])

        dbc.execute(u"""INSERT INTO contents
                    (docid, mail_from, mail_to, cc, bcc, subject, body)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (self.msgid, self.sender, self.to, self.cc, self.bcc,
                     self.subject, self.body))


class CustomMaildir(object):

    def __init__(self, maildir):
        self.maildir = maildir

        # Make sure that all the directories are in place for the Maildir.
        try:
            os.makedirs(os.path.join(maildir, "cur"))
        except os.error:
            pass
        try:
            os.makedirs(os.path.join(maildir, "new"))
        except os.error:
            pass
        try:
            os.makedirs(os.path.join(maildir, "tmp"))
        except os.error:
            pass

    def path(self, gm_msgid, flags=[]):
        prefix = "new"
        if r"\Seen" in flags:
            prefix = "cur"

        path = os.path.join(self.maildir, prefix, str(gm_msgid))

        return path

    def add(self, gm_msgid, msg, flags=[]):
        with open(self.path(gm_msgid, flags=flags), "wb") as f:
            f.write(msg)

    def get(self, k, flags=[]):
        with open(self.path(k, flags=flags)) as f:
            msg = email.message_from_file(f)
        return msg


class IMAPFolder(object):

    parse = re.compile(r"\((.*?)\) \"(.*?)\" \"(.*?)\"")

    def __init__(self, spec):
        self.spec = spec
        match = self.parse.search(spec)
        if match is None:
            raise IMAPSyncError(u"Couldn't parse folder: '{0}'"
                                .format(spec))
        flags, self.delim, self.name = match.groups()
        self.flags = flags.split()

    def __str__(self):
        return self.name

    @property
    def noselect(self):
        return r"\Noselect" in self.flags


class IMAPConnection(object):
    """
    A wrapper around an :class:`imaplib.IMAP4_SSL` object that allows it to
    be used in a ``with`` block.

    """
    def __init__(self, email, password,
                 server=u"imap.gmail.com", port=993):
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


class IMAPDB(object):
    def __init__(self, fn):
        try:
            os.makedirs(os.path.dirname(fn))
        except os.error:
            pass

        self.fn = fn

    def __enter__(self):
        self.connection = sqlite3.connect(self.fn)
        self.connection.text_factory = str
        return self.connection.__enter__()

    def __exit__(self, *args):
        return self.connection.__exit__(*args)


if __name__ == "__main__":
    e = raw_input(u"Your email address: ")
    a = GmailAccount(e)
    a.fetch_all()
