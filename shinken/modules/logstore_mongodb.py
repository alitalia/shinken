# import von modules/livestatus_logstore


"""
This class is for attaching a sqlite database to a livestatus broker module.
It is one possibility for an exchangeable storage for log broks
"""

import os
import time
import datetime
import re
import pymongo
from shinken.objects.service import Service
from livestatus_broker.livestatus_stack import LiveStatusStack
from livestatus_broker.mapping import LOGCLASS_ALERT, LOGCLASS_PROGRAM, LOGCLASS_NOTIFICATION, LOGCLASS_PASSIVECHECK, LOGCLASS_COMMAND, LOGCLASS_STATE, LOGCLASS_INVALID, LOGOBJECT_INFO, LOGOBJECT_HOST, LOGOBJECT_SERVICE, Logline

from pymongo import Connection
from pymongo.errors import AutoReconnect


from shinken.basemodule import BaseModule
from shinken.objects.module import Module

properties = {
    'daemons' : ['livestatus'],
    'type' : 'logstore_mongodb',
    'external' : False,
    'phases' : ['running'],
    }


#called by the plugin manager
def get_instance(plugin):
    print "Get an LogStore MongoDB module for plugin %s" % plugin.get_name()
    instance = LiveStatusLogStoreMongoDB(plugin)
    return instance

def row_factory(cursor, row):
    """Handler for the sqlite fetch method."""
    return Logline(cursor.description, row)


class LiveStatusLogStoreError(Exception):
    pass


class LiveStatusLogStoreMongoDB(BaseModule):

    def __init__(self, modconf):
        BaseModule.__init__(self, modconf)
        self.plugins = []
        # mongodb://host1,host2,host3/?safe=true;w=2;wtimeoutMS=2000
        self.mongodb_uri = getattr(modconf, 'mongodb_uri', None)
        self.database = getattr(modconf, 'database', 'logs')
        self.use_aggressive_sql = True
        max_logs_age = getattr(modconf, 'max_logs_age', '365')
        maxmatch = re.match(r'^(\d+)([dwm]*)$', max_logs_age)
        if maxmatch is None:
            print 'Warning : wrong format for max_logs_age. Must be <number>[d|w|m|y] or <number> and not %s' % max_logs_age
            return None
        else:
            if not maxmatch.group(2):
                self.max_logs_age = int(maxmatch.group(1))
            elif maxmatch.group(2) == 'd':
                self.max_logs_age = int(maxmatch.group(1))
            elif maxmatch.group(2) == 'w':
                self.max_logs_age = int(maxmatch.group(1)) * 7
            elif maxmatch.group(2) == 'm':
                self.max_logs_age = int(maxmatch.group(1)) * 31
            elif maxmatch.group(2) == 'y':
                self.max_logs_age = int(maxmatch.group(1)) * 365

        # This stack is used to create a full-blown select-statement
        self.mongo_filter_stack = LiveStatusMongoStack()
        # This stack is used to create a minimal select-statement which
        # selects only by time >= and time <=
        self.mongo_time_filter_stack = LiveStatusMongoStack()
        self.is_connected = False
        # Now sleep one second, so that won't get lineno collisions with the last second
        time.sleep(1)
        self.lineno = 0

    def load(self, app):
        self.app = app

    def init(self):
        pass

    def open(self):
        print "open LiveStatusLogStoreMongoDB ok"
        try:
            self.conn = pymongo.Connection(self.mongodb_uri, fsync=True)
            self.db = self.conn[self.database]
            self.is_connected = True
        except AutoReconnect, exp:
            # now what, ha?
            print "LiveStatusLogStoreMongoDB.AutoReconnect", exp
            raise
            pass

    def close(self):
        self.conn.disconnect()

    def commit(self):
        pass

    def do_i_need_this_manage_brok(self, brok):
        """ Look for a manager function for a brok, and call it """
        manage = getattr(self, 'manage_' + brok.type + '_brok', None)
        if manage:
            return manage(brok)

    def manage_log_brok(self, b):
        data = b.data
        line = data['log']
        try:
            logline = Logline(line=line)
            values = logline.as_dict()
        except Exception, exp:
            print "Unexpected error:", exp
        try:
            if logline.logclass != LOGCLASS_INVALID:
                self.db.logs.insert(values)
        except Exception, exp:
            print "An error occurred:", exp
            print "DATABASE ERROR!!!!!!!!!!!!!!!!!"
        #FIXME need access to this#self.livestatus.count_event('log_message')

    def add_filter(self, operator, attribute, reference):
	if attribute == 'time':
	    self.mongo_time_filter_stack.put_stack(self.make_mongo_filter(operator, attribute, reference))
	self.mongo_filter_stack.put_stack(self.make_mongo_filter(operator, attribute, reference))

    def add_filter_and(self, andnum):
	self.mongo_filter_stack.and_elements(andnum)

    def add_filter_or(self, ornum):
	self.mongo_filter_stack.or_elements(ornum)

    def get_live_data_log(self):
        """Like get_live_data, but for log objects"""
        # finalize the filter stacks
	self.mongo_time_filter_stack.and_elements(self.mongo_time_filter_stack.qsize())
	self.mongo_filter_stack.and_elements(self.mongo_filter_stack.qsize())
        self.use_aggressive_sql = True
        if self.use_aggressive_sql:
            # Be aggressive, get preselected data from sqlite and do less
            # filtering in python. But: only a subset of Filter:-attributes
            # can be mapped to columns in the logs-table, for the others
            # we must use "always-true"-clauses. This can result in
            # funny and potentially ineffective sql-statements
            mongo_filter_func = self.mongo_filter_stack.get_stack()
        else:
            # Be conservative, get everything from the database between
            # two dates and apply the Filter:-clauses in python
            mongo_filter_func = self.mongo_time_filter_stack.get_stack()
        result = []
        mongo_filter = mongo_filter_func()
        print "mongo filter is", mongo_filter
        # We can apply the filterstack here as well. we have columns and filtercolumns.
        # the only additional step is to enrich log lines with host/service-attributes
        # A timerange can be useful for a faster preselection of lines
        filter_element = eval(mongo_filter)
        print "mongo filter iis", type(filter_element)
        print "mongo filter iis", filter_element
        dbresult = []
        columns = ['logobject', 'attempt', 'logclass', 'command_name', 'comment', 'contact_name', 'host_name', 'lineno', 'message', 'options', 'plugin_output', 'service_description', 'state', 'state_type', 'time', 'type']
        if not self.is_connected:
            print "sorry, not connected"
        else:
            dbresult = [Logline([(c, ) for c in columns], [x[col] for col in columns]) for x in self.db.logs.find(filter_element)]
        return dbresult

    def make_mongo_filter(self, operator, attribute, reference):
        # The filters are text fragments which are put together to form a sql where-condition finally.
        # Add parameter Class (Host, Service), lookup datatype (default string), convert reference
        # which attributes are suitable for a sql statement
        good_attributes = ['time', 'attempt', 'class', 'command_name', 'comment', 'contact_name', 'host_name', 'plugin_output', 'service_description', 'state', 'state_type', 'type']
        good_operators = ['=', '!=']
        #  put strings in '' for the query
        if attribute in ['command_name', 'comment', 'contact_name', 'host_name', 'plugin_output', 'service_description', 'state_type', 'type']:
            attribute = "'%s'" % attribute

        def eq_filter():
            if reference == '':
                return '\'%s\' : \'\'' % (attribute,)
            else:
                return '\'%s\' : %s' % (attribute, reference)
        def ne_filter():
            if reference == '':
                return '\'%s\' : { \'$ne\' : '' }' % (attribute,)
            else:
                return '\'%s\' : { \'$ne\' : %s }' % (attribute, reference)
        def gt_filter():
            return '\'%s\' : { \'$gt\' : %s }' % (attribute, reference)
        def ge_filter():
            return '\'%s\' : { \'$gte\' : %s }' % (attribute, reference)
        def lt_filter():
            return '\'%s\' : { \'$lt\' : %s }' % (attribute, reference)
        def le_filter():
            return '\'%s\' : { \'$lte\' : %s }' % (attribute, reference)
        def match_filter():
            return '\'%s\' : { \'$regex\' : \'%s\' }' % (attribute, reference)
        def no_filter():
            return '{}'
        if attribute not in good_attributes:
            return no_filter
        if operator == '=':
            return eq_filter
        if operator == '>':
            return gt_filter
        if operator == '>=':
            return ge_filter
        if operator == '<':
            return lt_filter
        if operator == '<=':
            return le_filter
        if operator == '!=':
            return ne_filter
        if operator == '~':
            return match_filter


class LiveStatusMongoStack(LiveStatusStack):
    """A Lifo queue for filter functions.

    This class inherits either from MyLifoQueue or Queue.LifoQueue
    whatever is available with the current python version.

    Public functions:
    and_elements -- takes a certain number (given as argument)
    of filters from the stack, creates a new filter and puts
    this filter on the stack. If these filters are lambda functions,
    the new filter is a boolean and of the underlying filters.
    If the filters are sql where-conditions, they are also concatenated
    with and to form a new string containing a more complex where-condition.

    or_elements --- the same, only that the single filters are
    combined with a logical or.

    """

    def __init__(self, *args, **kw):
        self.type = 'mongo'
        self.__class__.__bases__[0].__init__(self, *args, **kw)

    def and_elements(self, num):
        """Take num filters from the stack, and them and put the result back"""
        if num > 1:
            filters = []
            for _ in range(num):
                filters.append(self.get_stack())
            # Take from the stack:
            # Make a combined anded function
            # Put it on the stack
            print "filter is", filters
            and_clause = lambda: '{\'$and\' : [%s]}' % ', '.join('{ ' + x() + ' }' for x in filters)
            print "and_elements", and_clause
            self.put_stack(and_clause)

    def or_elements(self, num):
        """Take num filters from the stack, or them and put the result back"""
        if num > 1:
            filters = []
            for _ in range(num):
                filters.append(self.get_stack())
            or_clause = lambda: '{\'$or\' : [%s]}' % ', '.join('{ ' + x() + ' }' for x in filters)
            print "or_elements", or_clause
            self.put_stack(or_clause)

    def get_stack(self):
        """Return the top element from the stack or a filter which is always true"""
        if self.qsize() == 0:
            return lambda: ''
        else:
            return self.get()