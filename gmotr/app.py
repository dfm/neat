# vim: set fileencoding=utf-8 :


import locale
locale.setlocale(locale.LC_ALL, "")
code = locale.getpreferredencoding()

import re
import curses
import textwrap
from email.header import decode_header

from datetime import datetime, timedelta, tzinfo
import time as _time

from imap_utils import GmailAccount


class MessageInfo(object):

    parse_sender = re.compile(r"(.*?) <(.*?)>")

    def __init__(self, doc):
        self.doc = doc
        self.sender, self.email = self.parse_email(self.doc.get(u"from", u""))
        self.to, self.tomail = self.parse_email(self.doc.get(u"to", u""))

    def parse_email(self, field):
        match = self.parse_sender.search(field)
        if match is None:
            try:
                res = field.split("@")
                res = res[0].strip().strip("<")
            except:
                res = u"Unknown"
            match = [res, field]
        else:
            match = match.groups()
        return match

    @property
    def unread(self):
        return u"\\Seen" not in self.doc.get(u"flags")

    @property
    def important(self):
        return u"\"\\\\Important\"" in self.doc.get(u"labels")

    @property
    def sent(self):
        return u"\"\\\\Sent\"" in self.doc.get(u"labels")

    def to_str(self, width, from_width=25):
        s = u" "

        # Flags and labels.
        if self.unread:
            s += u"o "
        else:
            s += u"  "

        if self.important:
            s += u"> "
        else:
            s += u"  "

        # From sender.
        if self.sent:
            v, cs = decode_header(self.to)[0]
            if cs is None:
                cs = u"utf-8"
            from_str = u"To: " + unicode(v, cs).strip("\"' ")
        else:
            v, cs = decode_header(self.sender)[0]
            if cs is None:
                cs = u"utf-8"
            from_str = unicode(v, cs).strip("\"' ")

        s += u"{{0:{0}s}}  ".format(from_width).format(from_str[:from_width])

        # Subject.
        v, cs = decode_header(self.doc.get(u"subject", u"(No subject)"))[0]
        if cs is None:
            cs = u"utf-8"
        s += unicode(v.strip(), cs)

        # Parse the date.
        date = datetime.utcfromtimestamp(self.doc.get(u"time")) \
                        .replace(tzinfo=UTC()).astimezone(LocalTimezone())
        dt = datetime.now(UTC()) - date

        if dt.days == 0 and dt.seconds < 43200:
            date = date.strftime(u"%I:%M %p").lower()
            if date[0] == u"0":
                date = date[1:]
        else:
            date = date.strftime(u"%b %d")

        # Compute the layout.
        size = width - 2 - len(date)
        return (u"{{0:{0}s}} {{1}}".format(size).format(s[:size], date)) \
                .encode(code)

    @property
    def color(self):
        if self.important:
            return curses.COLOR_CYAN
        if self.unread:
            return curses.COLOR_BLUE
        return curses.COLOR_BLACK


class MessageDetail(object):

    def __init__(self, doc):
        self.doc = doc
        self.info = MessageInfo(doc)

    def to_str(self, width):
        msg = self.doc.get(u"message", u"")

        # Iterate through the parts and deal with encodings.
        s = u""
        charsets = msg.get_charsets()
        for i, p in enumerate(msg.walk()):
            ct = p.get_content_type()
            if ct == "text/plain":
                cs = charsets[i]
                if cs is None:
                    cs = u"utf-8"
                s += unicode(p.get_payload(decode=True), cs)
                s += u"\n\n"

            elif u"multipart" not in ct:
                s += u"=== Part {0}: Content-type: {1} ===\n\n\n".format(i + 1,
                                                                        ct)

        r = []

        # Build the header.
        for k in [u"From", u"To", u"Cc", u"Bcc", u"Subject"]:
            tmp = []
            matches = msg.get_all(k)
            if matches is not None:
                els = []
                for m in matches:
                    els += m.split(u",")
                for el in els:
                    v, cs = decode_header(el.strip())[0]
                    if cs is None:
                        cs = u"utf-8"
                    tmp.append(unicode(v, cs))

                t = k + u": "
                r += [u"\n".join(textwrap.wrap(t + u", ".join(tmp),
                                            width - 1,
                                            subsequent_indent=u" " * len(t)))]

        r += [u"", u""]

        # Wrap the text properly.
        for l in s.split(u"\n"):
            r += [u"\n".join([unicode(nl, code)
                                    for nl in textwrap.wrap(l.encode(code),
                                                            width - 1)])]

        return u"\n".join(r)


class Mailbox(object):

    def __init__(self, acct):
        self._acct = acct
        self._messages = []

        self.reset()

    def reset(self):
        self._selected = 0

    def scroll(self, n):
        self._selected = max(min(self._selected + n,
                                 len(self._messages) - 1), 0)

    def search(self, q=None):
        self._messages = [MessageInfo(m) for m in acct.simple_list(q=q)]

    def fetch_selected(self):
        msg = self.selected
        uid = msg.doc["uid"]
        return MessageDetail(acct.fetch_message(uid))

    @property
    def selected(self):
        return self._messages[self._selected]

    def __getitem__(self, i):
        return self._messages[i]

    def __len__(self):
        return len(self._messages)


class GMOTRApp(object):

    def __init__(self, email_address, acct):
        self._email = email_address
        self.mailbox = Mailbox(acct)
        self.message = None

    def run(self):
        self.mailbox.search()
        curses.wrapper(self)

    def __call__(self, screen):
        # Extra curses setup.
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.curs_set(0)

        # Save a reference to the main screen.
        self.screen = screen

        # Draw the sub-windows.
        self.draw_windows()

        curses.doupdate()

        # This is a placeholder for use with repeatable commands.
        repeat = ""

        # Start the main event loop.
        mode = "list"
        while 1:
            # Wait for a keystroke.
            c = self.listview.getch()

            # Try to parse the repeats string.
            try:
                reps = int(repeat)
            except ValueError:
                reps = 1

            if mode == "list":
                if c == ord("q"):
                    break

                elif c == ord("j") or c == curses.KEY_DOWN:
                    self.scroll(reps)

                elif c == ord("k") or c == curses.KEY_UP:
                    self.scroll(-reps)

                elif c == ord("\n"):
                    mode = "message"
                    self.update_toolbar("Fetching message...")
                    curses.doupdate()
                    self.message = self.mailbox.fetch_selected()
                    self.update_toolbar("Complete.")
                    self.display_selected()

                elif c == ord(":"):
                    q = self.get_input(u":")
                    self.update_toolbar("Searching for: '{0}' ...".format(q))
                    curses.doupdate()

                    # Run the IMAP query.
                    self.mailbox.search(q)

                    # Display the results.
                    self.update_toolbar("Found {0} results for: '{1}'"
                                        .format(len(self.mailbox), q))
                    self.update_listview()

            elif mode == "message":

                if c == ord("q"):
                    mode = "list"
                    self.scroll(0)

                elif c == ord("j") or c == curses.KEY_DOWN:
                    self.scroll_message(reps)

                elif c == ord("k") or c == curses.KEY_UP:
                    self.scroll_message(-reps)

            if c in range(ord("0"), ord("9")):
                repeat += chr(c)
                self.update_statusbar(reps=repeat)

            else:
                repeat = ""

            # Re-draw the UI anywhere where it is needed.
            curses.doupdate()

    def draw_windows(self):
        self.height, self.width = self.screen.getmaxyx()

        # Draw the message display view.
        self.messageview = curses.newpad(self.height - 3, self.width)
        self.messageview.keypad(1)

        # Draw the list view.
        self.listview = curses.newpad(self.height - 3, self.width)
        self.listview.keypad(1)
        self.update_listview()

        # Draw the status bar near the bottom...
        self.statusbar = self.screen.subwin(1, self.width, self.height - 2, 0)
        self.statusbar.bkgd(" ", curses.color_pair(2))
        self.statusbar.attron(curses.A_BOLD)
        self.update_statusbar()

        # ...and the tool bar along the very bottom.
        self.toolbar = self.screen.subwin(1, self.width, self.height - 1, 0)
        self.toolbar.bkgd(" ", curses.color_pair(1))
        self.update_toolbar()

    def update_statusbar(self, reps=None):
        self.statusbar.erase()
        self.statusbar.addstr(0, 0, u"[{0}]".format(self._email))
        if reps is not None:
            reps = str(reps)
            self.statusbar.addstr(0, self.width - len(reps) - 1, reps)
        self.statusbar.noutrefresh()

    def update_toolbar(self, contents=u""):
        self.toolbar.erase()
        self.toolbar.addstr(0, 0, contents)
        self.toolbar.noutrefresh()

    def update_listview(self):
        self.mailbox.reset()
        self._scroll_pos = 0

        self.listview.erase()
        self.listview.resize(max(len(self.mailbox), self.height - 2),
                             self.width)

        self._rows = []
        for i, msg in enumerate(self.mailbox):
            row = self.listview.subwin(1, self.width, i, 0)

            if msg.unread:
                row.bkgd(" ", curses.color_pair(2))
            elif msg.important:
                row.bkgd(" ", curses.A_BOLD)

            row.addstr(0, 0, msg.to_str(self.width))
            self._rows.append(row)

        self.scroll(0)

    def scroll(self, ind):
        # Remove the styling from the previously selected row.
        row = self._rows[self.mailbox._selected]
        msg = self.mailbox.selected

        if msg.unread:
            row.bkgd(" ", curses.color_pair(2))
        elif msg.important:
            row.bkgd(" ", curses.A_BOLD)
        else:
            row.bkgd(" ")

        # Compute the new selection.
        self.mailbox.scroll(ind)

        # Add the styles to the new selection.
        row = self._rows[self.mailbox._selected]
        row.bkgd(" ", curses.color_pair(3))

        # Update the scroll position if needed.
        if self.mailbox._selected - self._scroll_pos >= self.height - 3:
            self._scroll_pos = self.mailbox._selected - self.height + 3
        elif self.mailbox._selected - self._scroll_pos < 0:
            self._scroll_pos = self.mailbox._selected

        self.listview.noutrefresh(self._scroll_pos, 0, 0, 0,
                              self.height - 3, self.width)

    def get_input(self, msg):
        self.update_toolbar(contents=msg)
        self.toolbar.refresh()

        curses.curs_set(1)
        curses.echo(1)
        result = self.toolbar.getstr()
        curses.echo(0)
        curses.curs_set(0)

        return result

    def display_selected(self):
        # self.mailbox.reset()
        self._message_scroll_pos = 0

        contents = self.message.to_str(self.width)
        self._nlines = len(contents.splitlines())

        self.messageview.erase()
        self.messageview.resize(max(self._nlines, self.height - 2),
                                self.width)

        self.messageview.addstr(0, 0, contents.encode(code))
        self.scroll_message(0)

    def scroll_message(self, ind):
        self._message_scroll_pos = min(self._nlines - self.height + 2, max(0,
                                   self._message_scroll_pos + ind))
        self.messageview.refresh(self._message_scroll_pos, 0, 0, 0,
                                 self.height - 3, self.width)


#
# TIMEZONE HACKS.
#

STDOFFSET = timedelta(seconds=-_time.timezone)
if _time.daylight:
    DSTOFFSET = timedelta(seconds=-_time.altzone)
else:
    DSTOFFSET = STDOFFSET

DSTDIFF = DSTOFFSET - STDOFFSET
ZERO = timedelta(0)


class UTC(tzinfo):

    def utcoffset(self, dt):
        return ZERO

    def tzname(self, dt):
        return u"UTC"

    def dst(self, dt):
        return ZERO


class LocalTimezone(tzinfo):

    def utcoffset(self, dt):
        if self._isdst(dt):
            return DSTOFFSET
        else:
            return STDOFFSET

    def dst(self, dt):
        if self._isdst(dt):
            return DSTDIFF
        else:
            return ZERO

    def tzname(self, dt):
        return _time.tzname[self._isdst(dt)]

    def _isdst(self, dt):
        tt = (dt.year, dt.month, dt.day,
              dt.hour, dt.minute, dt.second,
              dt.weekday(), 0, 0)
        stamp = _time.mktime(tt)
        tt = _time.localtime(stamp)
        return tt.tm_isdst > 0


if __name__ == "__main__":
    # e = raw_input(u"Enter your email address: ")
    e = u"foreman.mackey@gmail.com"
    acct = GmailAccount(e)
    # print([MessageInfo(m).to_str(100) for m in acct.simple_list()])
    # assert 0
    app = GMOTRApp(e, acct)
    app.run()
