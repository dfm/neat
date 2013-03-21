#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import (division, print_function, absolute_import,
                        unicode_literals)

__all__ = ["GmailMessage", "GmailAccount", "GmailAddress", "GmailFlag",
           "GmailLabel", "SMTPAccount"]

import os
import re
from sqlalchemy import (Table, Column, Integer, String, Text, DateTime,
                        ForeignKey, create_engine)
from sqlalchemy.orm import relationship, backref, sessionmaker, deferred
from sqlalchemy.ext.declarative import declarative_base


# Regular expression for parsing email addresses.
email_re = re.compile(r"(.*?)(?:(?:<(.*?)>)|$)")


def parse_email(rep):
    result = email_re.search(rep)
    assert result is not None
    name, email = result.groups()
    if email is None:
        return None, name
    return name, email


# The Session.
DB_URI = os.environ.get("GMOTR_DB_URI", "postgresql+psycopg2:///gmotr")
engine = create_engine(DB_URI)
Session = sessionmaker(bind=engine)

# Base class for the models.
Base = declarative_base()


def create_all():
    Base.metadata.create_all(engine)


def drop_all():
    Base.metadata.drop_all(engine)


# Relationships.
to_addresses = Table("to_addresses", Base.metadata,
                     Column("message_id", Integer, ForeignKey("messages.id")),
                     Column("address_id", Integer, ForeignKey("addresses.id")))

cc_addresses = Table("cc_addresses", Base.metadata,
                     Column("message_id", Integer, ForeignKey("messages.id")),
                     Column("address_id", Integer, ForeignKey("addresses.id")))

bcc_addresses = Table("bcc_addresses", Base.metadata,
                      Column("message_id", Integer, ForeignKey("messages.id")),
                      Column("address_id", Integer,
                             ForeignKey("addresses.id")))

message_flags = Table("message_flags", Base.metadata,
                      Column("message_id", Integer, ForeignKey("messages.id")),
                      Column("flag_id", Integer, ForeignKey("flags.id")))

message_labels = Table("message_labels", Base.metadata,
                       Column("message_id", Integer,
                              ForeignKey("messages.id")),
                       Column("label_id", Integer, ForeignKey("labels.id")))


class GmailMessage(Base):

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    uid = Column(Integer, unique=True)
    message_id = Column(Integer, unique=True)
    thread_id = Column(Integer, unique=True)

    account_id = Column(Integer, ForeignKey("accounts.id"))
    account = relationship("GmailAccount", backref=backref("messages"))

    internaldate = Column(DateTime)
    date = Column(String)

    subject = Column(String)
    body = deferred(Column(Text))

    from_address_id = Column(Integer, ForeignKey("addresses.id"))
    from_address = relationship("GmailAddress")
    to_addresses = relationship("GmailAddress", secondary=to_addresses)
    cc_addresses = relationship("GmailAddress", secondary=cc_addresses)
    bcc_addresses = relationship("GmailAddress", secondary=bcc_addresses)

    flags = relationship("GmailFlag", secondary=message_flags)
    labels = relationship("GmailLabel", secondary=message_labels)

    def __init__(self, uid, message_id, thread_id, account, internaldate,
                 date, subject, from_address, to_addresses, cc_addresses,
                 bcc_addresses, flags, labels, body=None):
        self.uid = int(uid)
        self.message_id = int(message_id)
        self.thread_id = int(thread_id)

        self.account = account

        self.internaldate = internaldate
        self.date = date

        self.subject = subject
        self.body = body

        self.from_address = from_address
        self.to_addresses = to_addresses
        self.cc_addresses = cc_addresses
        self.bcc_addresses = bcc_addresses

        self.flags = flags
        self.labels = labels

    def __repr__(self):
        return ("<GmailMessage({0.message_id})>").format(self)


class GmailAccount(Base):

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)

    email = Column(String, nullable=False, unique=True)
    name = Column(String)

    last_checked = Column(DateTime)
    most_recent_uid = Column(Integer)

    def __init__(self, email, name="", last_checked=None, most_recent_uid=0):
        self.email = email
        self.name = name
        self.last_checked = None
        self.most_recent_uid = most_recent_uid

    def __repr__(self):
        return ("<GmailAccount(\"{0.email}\", name=\"{0.name}\", "
                "last_checked={0.last_checked}, most_recent_uid="
                "{0.most_recent_uid})>").format(self)


class SMTPAccount(Base):

    __tablename__ = "smtp_accounts"

    id = Column(Integer, primary_key=True)

    email = Column(String, nullable=False, unique=True)
    name = Column(String)
    server = Column(String, nullable=False)
    port = Column(Integer, nullable=False)

    signature = deferred(Column(Text))


class GmailAddress(Base):

    __tablename__ = "addresses"

    id = Column(Integer, primary_key=True)

    name = Column(String)
    email = Column(String)
    raw = Column(String)

    def __init__(self, rep):
        self.raw = rep
        self.name, self.email = parse_email(rep)

    def __repr__(self):
        return "<GmailAddress(\"{}\")>".format(self.raw)


class GmailLabel(Base):

    __tablename__ = "labels"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "<GmailLabel(\"{}\")>".format(self.name)


class GmailFlag(Base):

    __tablename__ = "flags"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "<GmailFlag(\"{}\")>".format(self.name)
