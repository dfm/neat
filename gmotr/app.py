# vim: set fileencoding=utf-8 :

import re
import curses
from email.header import decode_header

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

    def to_str(self, width, from_width=20):
        s = u""

        # Flags and labels.
        if self.unread:
            s += u"* "
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
        v, cs = decode_header(self.doc.get("subject", "(No subject)"))[0]
        if cs is None:
            cs = u"utf-8"
        s += unicode(v.strip(), cs)

        return s[:width - 1].encode("ascii", "replace")

    @property
    def color(self):
        if self.important:
            return curses.COLOR_CYAN
        if self.unread:
            return curses.COLOR_BLUE
        return curses.COLOR_BLACK


class GMOTRApp(object):

    def __init__(self, email_address, acct):
        self._email = email_address
        self._acct = acct
        self._messages = [MessageInfo(m) for m in acct.simple_list()]

    def run(self):
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
        while 1:
            # Wait for a keystroke.
            c = self.listview.getch()

            # Try to parse the repeats string.
            try:
                reps = int(repeat)
            except ValueError:
                reps = 1

            if c == ord("q"):
                break

            elif c == ord("j") or c == curses.KEY_DOWN:
                self.scroll(reps)
                repeat = ""

            elif c == ord("k") or c == curses.KEY_UP:
                self.scroll(-reps)
                repeat = ""

            elif c in range(ord("0"), ord("9")):
                repeat += chr(c)
                self.update_statusbar(reps=repeat)

            elif c == ord("/"):
                q = self.get_input(u"/")
                self._messages = [MessageInfo(m)
                                        for m in acct.simple_list(q=q)]
                self.update_listview()

            else:
                repeat = ""

            # Re-draw the UI anywhere where it is needed.
            curses.doupdate()

    def draw_windows(self):
        self.height, self.width = self.screen.getmaxyx()

        # Draw the list view.
        self.listview = curses.newpad(len(self._messages), self.width)
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
        self._selected = 0
        self._scroll_pos = 0

        self.listview.erase()
        self.listview.resize(max(len(self._messages), self.height - 3),
                             self.width)

        self._rows = []
        for i, msg in enumerate(self._messages):
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
        row = self._rows[self._selected]
        msg = self._messages[self._selected]

        if msg.unread:
            row.bkgd(" ", curses.color_pair(2))
        elif msg.important:
            row.bkgd(" ", curses.A_BOLD)
        else:
            row.bkgd(" ")

        # Compute the new selection.
        self._selected = max(min(self._selected + ind,
                                 len(self._messages) - 1), 0)

        # Add the styles to the new selection.
        row = self._rows[self._selected]
        row.bkgd(" ", curses.color_pair(3))

        # Update the scroll position if needed.
        if self._selected - self._scroll_pos >= self.height - 3:
            self._scroll_pos = self._selected - self.height + 3
        elif self._selected - self._scroll_pos < 0:
            self._scroll_pos = self._selected

        self.listview.refresh(self._scroll_pos, 0, 0, 0,
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


if __name__ == "__main__":
    # e = raw_input(u"Enter your email address: ")
    e = u"foreman.mackey@gmail.com"
    acct = GmailAccount(e)
    # print([MessageInfo(m).to_str(100) for m in acct.simple_list()])
    # assert 0
    app = GMOTRApp(e, acct)
    app.run()
