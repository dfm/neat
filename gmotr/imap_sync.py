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
import curses
import datetime

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

    """

    parse_uid = re.compile(r"UID ([0-9]*)")
    parse_x_gm_msgid = re.compile(r"X-GM-MSGID ([0-9]*)")
    parse_x_gm_thrid = re.compile(r"X-GM-THRID ([0-9]*)")
    parse_x_gm_labels = re.compile(r"X-GM-LABELS \((.*?)\)")
    parse_flags = re.compile(r"FLAGS \((.*?)\)")

    parse_labels = re.compile(r"")
    parse_labels = re.compile(r"(\"(?:.*?)\"|(?:.+?(?:\s|\Z)))")

    def __init__(self, email, password, maildir):
        self._imap = IMAPConnection(email, password)

        # Initialize the Maildir.
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

    def _folders(self, connection):
        folders = []
        code, data = connection.list()
        if code != "OK":
            raise IMAPSyncError(u"LIST responded with: '{0}'".format(code))

        for spec in data:
            folders.append(IMAPFolder(spec))

        return folders

    def _fetch(self, mb, mbname):
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
                sys.stdout.write(u"| " + u"|" * nfilled
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
        self.subject = u" ".join(self.msg.get_all(u"subject", u""))
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
    e = "foreman.mackey@gmail.com"
    a = GmailAccount(e, keyring.get_password("gob", e),
                     os.path.expanduser("~/.gmotr/mail"))
    a.fetch_all()
