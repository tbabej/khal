# vim: set ts=4 sw=4 expandtab sts=4 fileencoding=utf-8:
# Copyright (c) 2013-2014 Christian Geier et al.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""
The SQLite backend implementation.

Database Layout
===============

"""

from __future__ import print_function

import contextlib
import datetime
from os import makedirs, path
import sqlite3
import time

import icalendar
import pytz
import xdg.BaseDirectory

from .event import Event
from . import aux
from .. import log
from .exceptions import CouldNotCreateDbDir, OutdatedDbVersionError, \
    UpdateFailed

logger = log.logger

DB_VERSION = 3  # The current db layout version

RECURRENCE_ID = 'RECURRENCE-ID'
THISANDFUTURE = 'THISANDFUTURE'
THISANDPRIOR = 'THISANDPRIOR'


# TODO fix that event/vevent mess


class SQLiteDb(object):
    """
    This class should provide a caching database for a calendar, keeping raw
    vevents in one table but allowing to retrieve events by dates (via the help
    of some auxiliary tables)

    :param calendar: the `name` of this calendar, if the same *name* and
                     *dbpath* is given on next creation of an SQLiteDb object
                     the same tables will be used
    :type calendar: str
    :param db_path: path where this sqlite database will be saved, if this is
                    None, a place according to the XDG specifications will be
                    chosen
    :type db_path: str or None
    """

    def __init__(self, calendar, db_path, locale):
        if db_path is None:
            db_path = xdg.BaseDirectory.save_data_path('khal') + '/khal.db'
        self.db_path = path.expanduser(db_path)
        self.calendar = calendar
        self._create_dbdir()
        self.locale = locale
        self.table_m = calendar + '_m'
        self.table_d = calendar + '_d'
        self.table_dt = calendar + '_dt'
        self._at_once = False
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        self._create_default_tables()
        self._check_table_version()
        self._check_calendar_exists()

    @contextlib.contextmanager
    def at_once(self):
        self._at_once = True
        try:
            yield self
        except:
            raise
        else:
            self.conn.commit()
        finally:
            self._at_once = False

    def _create_dbdir(self):
        """create the dbdir if it doesn't exist"""
        if self.db_path == ':memory:':
            return None
        dbdir = self.db_path.rsplit('/', 1)[0]
        if not path.isdir(dbdir):
            try:
                logger.debug('trying to create the directory for the db')
                makedirs(dbdir, mode=0o770)
                logger.debug('success')
            except OSError as error:
                logger.fatal('failed to create {0}: {1}'.format(dbdir, error))
                raise CouldNotCreateDbDir()

    def _check_table_version(self):
        """tests for curent db Version
        if the table is still empty, insert db_version
        """
        self.cursor.execute('SELECT version FROM version')
        result = self.cursor.fetchone()
        if result is None:
            self.cursor.execute('INSERT INTO version (version) VALUES (?)',
                                (DB_VERSION, ))
            self.conn.commit()
        elif not result[0] == DB_VERSION:
            raise OutdatedDbVersionError(
                str(self.db_path) +
                " is probably an invalid or outdated database.\n"
                "You should consider removing it and running khal again.")

    def _create_default_tables(self):
        """creates version and calendar tables and inserts table version number
        """
        self.cursor.execute('CREATE TABLE IF NOT EXISTS '
                            'version (version INTEGER)')
        logger.debug("created version table")

        self.cursor.execute('''CREATE TABLE IF NOT EXISTS calendars (
            calendar TEXT NOT NULL UNIQUE,
            resource TEXT NOT NULL,
            ctag FLOAT
            )''')
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS events (
                href TEXT NOT NULL,
                hrefrecuid TEXT NOT NULL,
                calendar TEXT NOT NULL,
                sequence INT,
                etag TEXT,
                type TEXT,
                item TEXT,
                primary key (hrefrecuid, calendar)
                );''')
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS recs_loc (
            dtstart INT NOT NULL,
            dtend INT NOT NULL,
            href TEXT NOT NULL REFERENCES events( href ),
            hrefrecuid TEXT NOT NULL REFERENCES events( hrefrecuid ),
            recuid TEXT NOT NULL,
            primary key (href, recuid)
            );''')
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS recs_float (
            dtstart INT NOT NULL,
            dtend INT NOT NULL,
            href TEXT NOT NULL REFERENCES events( href ),
            hrefrecuid TEXT NOT NULL REFERENCES events( hrefrecuid ),
            recuid TEXT NOT NULL,
            primary key (href, recuid)
            );''')
        self.conn.commit()

    def _check_calendar_exists(self):
        """make sure an entry for the current calendar exists in `calendar`
        table
        """
        self.cursor.execute('''SELECT count(*) FROM calendars
                WHERE calendar = ?;''', (self.calendar,))
        result = self.cursor.fetchone()

        if result[0] != 0:
            logger.debug("tables for calendar {0} exist".format(self.calendar))
        else:
            sql_s = 'INSERT INTO calendars (calendar, resource) VALUES (?, ?);'
            stuple = (self.calendar, '')
            self.sql_ex(sql_s, stuple)

    def sql_ex(self, statement, stuple=''):
        """wrapper for sql statements, does a "fetchall" """
        self.cursor.execute(statement, stuple)
        result = self.cursor.fetchall()
        if not self._at_once:
            self.conn.commit()
        return result

    def update(self, vevent, href, etag=''):
        """insert a new or update an existing card in the db

        This is mostly a wrapper around two SQL statements, doing some cleanup
        before.

        :param vevent: event to be inserted or updated. If this is a calendar
                       object, it will be searched for an event.
        :type vevent: unicode
        :param href: href of the card on the server, if this href already
                     exists in the db the card gets updated. If no href is
                     given, a random href is chosen and it is implied that this
                     card does not yet exist on the server, but will be
                     uploaded there on next sync.
        :type href: str()
        :param etag: the etag of the vcard, if this etag does not match the
                     remote etag on next sync, this card will be updated from
                     the server. For locally created vcards this should not be
                     set
        :type etag: str()
        """
        if href is None:
            raise ValueError('href may not be None')

        if isinstance(vevent, icalendar.cal.Event):
            ical = vevent
        else:
            ical = icalendar.Event.from_ical(vevent)

        # insert the (sub) events in the right order, e.g. recurrence-id events
        # after the corresponding rrule event
        def sort_key(vevent):
            uid = str(vevent['UID'])
            rid = vevent.get(RECURRENCE_ID)
            if rid is None:
                return uid, 0
            rrange = rid.params.get('RANGE')
            if rrange == THISANDPRIOR:
                raise UpdateFailed(
                    'The parameter `THISANDPRIOR` is not (and will not be) '
                    'supported by khal (as applications supporting the latest '
                    'standard MUST NOT create those. Therefore event {} from '
                    'calendar {} will not be shown in khal'
                    .format(href, self.calendar)
                )
            elif rrange == THISANDFUTURE:
                # TODO XXX sort these events further
                return uid, 2
            else:
                return uid, 1

        vevents = (aux.sanitize(c) for c in ical.walk() if c.name == 'VEVENT')
        # Need to delete the whole event in case we are updating a
        # recurring event with an event which is either not recurring any
        # more or has EXDATEs, as those would be left in the recursion
        # tables. There are obviously better ways to achieve the same
        # result.
        self.delete(href)
        for vevent in sorted(vevents, key=sort_key):
            self._update_impl(vevent, href, etag)

    def _update_impl(self, vevent, href, etag):
        """expand (if needed) and insert non-reccuring and original recurring
        (those with an RRULE property"""
        # TODO FIXME this function is a steaming pile of shit
        # TODO better naming for rid and recuid, naming is really ambiguous
        # table columns might need a rename, too
        # perhaps rid -> rec_uid, recuid -> rec_inst

        rid = vevent.get(RECURRENCE_ID)
        if rid is None:
            rrange = None
        else:
            rrange = rid.params.get('RANGE')

        # testing on datetime.date won't work as datetime is a child of date
        all_day_event = not isinstance(vevent['DTSTART'].dt, datetime.datetime)
        if all_day_event:
            recs_table = 'recs_float'
        else:
            recs_table = 'recs_loc'

        thisandfuture = (rrange == THISANDFUTURE)
        if thisandfuture:
            start_shift, duration = calc_shift_deltas(vevent)
            if all_day_event:
                start_shift = start_shift.days
                duration = duration.days
            else:
                start_shift = start_shift.days * 3600 * 24 + start_shift.seconds
                duration = duration.days * 3600 * 24 + duration.seconds

        dtstartend = aux.expand(vevent, self.locale['default_timezone'], href)
        for dtstart, dtend in dtstartend:
            if all_day_event:
                dbstart = dtstart.strftime('%Y%m%d')
                dbend = dtend.strftime('%Y%m%d')
                if rid is not None:
                    recuid = rid.dt.strftime('%Y%m%d')
                    hrefrecuid = href + recuid
                else:
                    recuid = dbstart
                    hrefrecuid = href
            else:
                # TODO: extract non-Olson TZs from params['TZID']
                # perhaps better done in event/vevent or directly in icalendar
                if dtstart.tzinfo is None:
                    dtstart = self.locale['default_timezone'].localize(dtstart)
                if dtend.tzinfo is None:
                    dtend = self.locale['default_timezone'].localize(dtend)
                dbstart = aux.to_unix_time(dtstart)
                dbend = aux.to_unix_time(dtend)

                if rid is not None:
                    recstart = rid.dt
                    if recstart.tzinfo is None:
                        recstart = self.locale['default_timezone'].localize(recstart)
                    recstart = str(aux.to_unix_time(recstart))
                    recuid = recstart
                    hrefrecuid = href + recuid
                else:
                    recuid = dbstart
                    hrefrecuid = href

            if thisandfuture:
                recs_sql_s = (
                    'UPDATE {0} SET dtstart = dtstart + ?, dtend = dtstart + ?, hrefrecuid=? '
                    'WHERE recuid >= ?;'.format(recs_table))
                stuple = (start_shift, start_shift + duration, hrefrecuid,
                          recuid)
            else:
                recs_sql_s = (
                    'INSERT OR REPLACE INTO {0} (dtstart, dtend, href, hrefrecuid, recuid)'
                    'VALUES (?, ?, ?, ?, ?);'.format(recs_table))
                stuple = (dbstart, dbend, href, hrefrecuid, recuid)
            self.sql_ex(recs_sql_s, stuple)

        sql_s = ('INSERT INTO events '
                 '(item, etag, href, calendar, hrefrecuid) '
                 'VALUES (?, ?, ?, ?, ?);')
        stuple = (vevent.to_ical().decode('utf-8'),
                  etag, href, self.calendar, hrefrecuid)
        self.sql_ex(sql_s, stuple)

    def get_ctag(self):
        stuple = (self.calendar, )
        sql_s = 'SELECT ctag FROM calendars WHERE calendar = ?;'
        try:
            ctag = self.sql_ex(sql_s, stuple)[0][0]
            return ctag
        except IndexError:
            return None

    def set_ctag(self, ctag):
        stuple = (ctag, self.calendar, )
        sql_s = 'UPDATE calendars SET ctag = ? WHERE calendar = ?;'
        self.sql_ex(sql_s, stuple)
        self.conn.commit()

    def get_etag(self, href):
        """get etag for href

        type href: str()
        return: etag
        rtype: str()
        """
        sql_s = 'SELECT etag FROM events WHERE href = ? AND calendar = ?;'
        try:
            etag = self.sql_ex(sql_s, (href, self.calendar))[0][0]
            return etag
        except IndexError:
            return None

    def delete(self, href, etag=None):
        """
        removes the event from the db,
        returns nothing
        :param etag: only there for compatiblity with vdirsyncer's Storage,
                     we always delete
        """
        for table in ['recs_loc', 'recs_float']:
            sql_s = 'DELETE FROM {0} WHERE href = ?;'.format(table)
            self.sql_ex(sql_s, (href,))
        sql_s = 'DELETE FROM events WHERE href = ?;'
        self.sql_ex(sql_s, (href, ))

    def list(self):
        """
        :returns: list of (href, etag)
        """
        sql_s = 'SELECT href, etag FROM events WHERE calendar = ?;'
        return list(set(self.sql_ex(sql_s, (self.calendar, ))))

    def get_time_range(self, start, end):
        """returns
        :type start: datetime.datetime
        :type end: datetime.datetime
        """
        start = time.mktime(start.timetuple())
        end = time.mktime(end.timetuple())
        sql_s = ('SELECT recs_loc.hrefrecuid, dtstart, dtend FROM '
                 'recs_loc JOIN events ON recs_loc.hrefrecuid = events.hrefrecuid WHERE '
                 '(dtstart >= ? AND dtstart <= ? OR '
                 'dtend >= ? AND dtend <= ? OR '
                 'dtstart <= ? AND dtend >= ?) AND calendar = ?;')
        stuple = (start, end, start, end, start, end, self.calendar)
        result = self.sql_ex(sql_s, stuple)
        event_list = list()
        for hrefrecuid, start, end in result:
            start = pytz.UTC.localize(
                datetime.datetime.utcfromtimestamp(start))
            end = pytz.UTC.localize(datetime.datetime.utcfromtimestamp(end))
            event = self.get(hrefrecuid, start=start, end=end)
            event_list.append(event)
        return event_list

    def get_allday_range(self, start, end=None):
        # TODO type check on start and end
        # should be datetime.date not datetime.datetime
        strstart = start.strftime('%Y%m%d')
        if end is None:
            end = start + datetime.timedelta(days=1)
        strend = end.strftime('%Y%m%d')
        sql_s = ('SELECT recs_float.hrefrecuid, dtstart, dtend FROM '
                 'recs_float JOIN events ON recs_float.hrefrecuid = events.hrefrecuid WHERE '
                 '(dtstart >= ? AND dtstart < ? OR '
                 'dtend > ? AND dtend <= ? OR '
                 'dtstart <= ? AND dtend > ? ) AND calendar = ?;')
        stuple = (strstart, strend, strstart, strend, strstart, strend, self.calendar)
        result = self.sql_ex(sql_s, stuple)
        event_list = list()
        for hrefrecuid, start, end in result:
            start = time.strptime(str(start), '%Y%m%d')
            end = time.strptime(str(end), '%Y%m%d')
            start = datetime.date(start.tm_year, start.tm_mon, start.tm_mday)
            end = datetime.date(end.tm_year, end.tm_mon, end.tm_mday)
            event = self.get(hrefrecuid, start=start, end=end)
            event_list.append(event)
        return event_list

    def get(self, hrefrecuid, start=None, end=None):
        """returns the Event matching hrefrecuid, if start and end are given, a
        specific Event from a Recursion set is returned, otherwise the Event
        returned exactly as saved in the db
        """
        sql_s = 'SELECT href, etag, item FROM events WHERE hrefrecuid = ?;'
        result = self.sql_ex(sql_s, (hrefrecuid, ))
        href, etag, item = result[0]
        return Event(item,
                     locale=self.locale,
                     start=start,
                     end=end,
                     href=href,
                     calendar=self.calendar,
                     etag=etag,
                     recuid=hrefrecuid,
                     )


def calc_shift_deltas(event):
    """calculate an events duration and by how much its start time has shifted
    versus its recurrence-id time

    :param event: an event with an RECURRENCE-ID property
    :type event: icalendar.Event
    :returns: time shift and duration
    :rtype: (datetime.timedelta, datetime.timedelta)
    """
    start_shift = event['DTSTART'].dt - event['RECURRENCE-ID'].dt
    try:
        duration = event['DTEND'].dt - event['DTSTART'].dt
    except KeyError:
        duration = event['DURATION'].dt
    return start_shift, duration
