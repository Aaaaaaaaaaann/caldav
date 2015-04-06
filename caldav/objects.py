#!/usr/bin/env python
# -*- encoding: utf-8 -*-

"""
A "DAV object" is anything we get from the caldav server or push into the caldav server, notably principal, calendars and calendar events.
"""

import vobject
import io
import uuid
import re
from lxml import etree

from caldav.lib import error, vcal, url
from caldav.lib.url import URL
from caldav.elements import dav, cdav
from caldav.lib.python_utilities import to_unicode


class DAVObject(object):
    """
    Base class for all DAV objects.  Can be instantiated by a client
    and an absolute or relative URL, or from the parent object.
    """
    id = None
    url = None
    client = None
    parent = None
    name = None

    def __init__(self, client=None, url=None, parent=None, name=None, id=None, **extra):
        """
        Default constructor.

        Parameters:
         * client: A DAVClient instance
         * url: The url for this object.  May be a full URL or a relative URL.
         * parent: The parent object - used when creating objects
         * name: A displayname
         * id: The resource id (UID for an Event)
        """

        if client is None and parent is not None:
            client = parent.client
        self.client = client
        self.parent = parent
        self.name = name
        self.id = id
        self.extra_init_options = extra
        ## url may be a path relative to the caldav root
        if client and url:
            self.url = client.url.join(url)
        else:
            self.url = URL.objectify(url)

    @property
    def canonical_url(self):
        return str(self.url.unauth())

    def children(self, type=None):
        """
        List children, using a propfind (resourcetype) on the parent object,
        at depth = 1.
        """
        c = []

        depth = 1
        properties = {}

        props = [dav.ResourceType(), ]
        response = self._query_properties(props, depth)
        properties = self._handle_prop_response(response=response, props=props, type=type, what='tag')

        for path in list(properties.keys()):
            resource_type = properties[path][dav.ResourceType.tag]
            if resource_type == type or type is None:
                ## TODO: investigate the RFCs thoroughly - why does a "get 
                ## members of this collection"-request also return the collection URL itself?  
                ## And why is the strip_trailing_slash-method needed?  The collection URL 
                ## should always end with a slash according to RFC 2518, section 5.2.
                if self.url.strip_trailing_slash() != self.url.join(path).strip_trailing_slash():
                    c.append((self.url.join(path), resource_type))

        return c

    def _query_properties(self, props=[], depth=0):
        root = None
        # build the propfind request
        if len(props) > 0:
            prop = dav.Prop() + props
            root = dav.Propfind() + prop

        return self._query(root, depth)

    def _query(self, root=None, depth=0, query_method='propfind', url=None, expected_return_value=None):
        """
        This is an internal method for doing a query.  It's a
        result of code-refactoring work, attempting to consolidate
        similar-looking code into a common method.
        """
        if url is None:
            url = self.url
        body = ""
        if root:
            body = etree.tostring(root.xmlelement(), encoding="utf-8",
                                  xml_declaration=True)
        ret = getattr(self.client, query_method)(
            url, body, depth)
        if ret.status == 404:
            raise error.NotFoundError(ret.raw)
        if (
                (expected_return_value is not None and ret.status != expected_return_value) or
                ret.status >= 400):
            raise error.exception_by_method[query_method](ret.raw)
        return ret
        

    def _handle_prop_response(self, response, props=[], type=None, what='text'):
        """
        Internal method to massage an XML response into a dict.  (This method is a result of some code refactoring work, attempting to consolidate similar-looking code)
        """
        properties = {}
        # All items should be in a <D:response> element
        for r in response.tree.findall('.//' + dav.Response.tag):
            status = r.find('.//' + dav.Status.tag)
            if not status.text.endswith('200 OK'):
                if status.text.endswith('404 Not Found'):
                    ## Silently ignoring the 404
                    ## (we should probably spit out some debug logging here)
                    continue
                raise error.ReportError(response.raw) ## TODO: may be wrong error class

            href = r.find('.//' + dav.Href.tag).text
            properties[href] = {}
            for p in props:
                t = r.find(".//" + p.tag)
                if len(list(t)) > 0:
                    if type is not None:
                        val = t.find(".//" + type)
                    else:
                        val = t.find(".//*")
                    if val is not None:
                        val = getattr(val, what)
                    else:
                        val = None
                else:
                    val = t.text
                properties[href][p.tag] = val

        return properties

    def get_properties(self, props=[], depth=0):
        """
        Get properties (PROPFIND) for this object. Works only for
        properties, that don't have complex types.

        Parameters:
         * props = [dav.ResourceType(), dav.DisplayName(), ...]

        Returns:
         * {proptag: value, ...}
        """
        rc = None
        response = self._query_properties(props, depth)
        properties = self._handle_prop_response(response, props)
        path = self.url.path
        exchange_path = self.url.path + '/'

        if path in list(properties.keys()):
            rc = properties[path]
        elif exchange_path in list(properties.keys()):
            rc = properties[exchange_path]
        else:
            raise Exception("The CalDAV server you are using has "
                            "a problem with path handling.")

        return rc

    def set_properties(self, props=[]):
        """
        Set properties (PROPPATCH) for this object.

        Parameters:
         * props = [dav.DisplayName('name'), ...]

        Returns:
         * self
        """
        prop = dav.Prop() + props
        set = dav.Set() + prop
        root = dav.PropertyUpdate() + set

        r = self._query(root, query_method='proppatch')

        statuses = r.tree.findall(".//" + dav.Status.tag)
        for s in statuses:
            if not s.text.endswith("200 OK"):
                raise error.PropsetError(r.raw)

        return self

    def save(self):
        """
        Save the object. This is an abstract methed, that all classes
        derived .from DAVObject implement.

        Returns:
         * self
        """
        raise NotImplementedError()

    def delete(self):
        """
        Delete the object.
        """
        if self.url is not None:
            r = self.client.delete(self.url)

            #TODO: find out why we get 404
            if r.status not in (200, 204, 404):
                raise error.DeleteError(r.raw)

    def __str__(self):
        return str(self.url)

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.url)


class CalendarSet(DAVObject):
    def calendars(self):
        """
        List all calendar collections in this set.

        Returns:
         * [Calendar(), ...]
        """
        cals = []

        data = self.children(cdav.Calendar.tag)
        for c_url, c_type in data:
            cals.append(Calendar(self.client, c_url, parent=self))

        return cals

    def make_calendar(self, name=None, cal_id=None, supported_calendar_component_set=None):
        return Calendar(self.client, name=name, parent=self, id=cal_id, supported_calendar_component_set=supported_calendar_component_set).save()

    def calendar(self, name=None, cal_id=None):
        """
        The calendar method will return a calendar object.  It will not initiate any communication with the server.
        """
        return Calendar(self.client, name=name, parent = self,
                        url = self.url.join(cal_id), id=cal_id)


class Principal(DAVObject):
    """
    This class represents a DAV Principal. It doesn't do much, except
    keep track of the URLs for the calendar-home-set, etc.
    """
    def __init__(self, client=None, url=None):
        """
        url input is for backward compatibility and should normally be avoided.

        If url is not given, deduct principal path as well as calendar home set path from doing propfinds.
        """
        self.client = client
        self._calendar_home_set = None

        ## backwards compatibility.  
        if url is not None:
            self.url = client.url.join(URL.objectify(url))
        else:
            self.url = self.client.url
            cup = self.get_properties([dav.CurrentUserPrincipal()])
            self.url = self.client.url.join(URL.objectify(cup['{DAV:}current-user-principal']))

    def make_calendar(self, name=None, cal_id=None, supported_calendar_component_set=None):
        return self.calendar_home_set.make_calendar(name, cal_id, supported_calendar_component_set=supported_calendar_component_set)

    def calendar(self, name=None, cal_id=None):
        """
        The calendar method will return a calendar object.  It will not initiate any communication with the server.
        """
        return self.calendar_home_set.calendar(name, cal_id)

    @property
    def calendar_home_set(self):
        if not self._calendar_home_set:
            chs = self.get_properties([cdav.CalendarHomeSet()])
            self.calendar_home_set = chs['{urn:ietf:params:xml:ns:caldav}calendar-home-set']
        return self._calendar_home_set

    @calendar_home_set.setter
    def calendar_home_set(self, url):
        if isinstance(url, CalendarSet):
            self._calendar_home_set = url
            return
        sanitized_url = URL.objectify(url)
        if sanitized_url.hostname and sanitized_url.hostname != self.client.url.hostname:
            ## icloud (and others?) having a load balanced system, where each principal resides on one named host
            self.client.url = sanitized_url
        self._calendar_home_set = CalendarSet(self.client, self.client.url.join(sanitized_url))

    def calendars(self):
        return self.calendar_home_set.calendars()

class Calendar(DAVObject):
    """
    The `Calendar` object is used to represent a calendar collection.
    Refer to the RFC for details: http://www.ietf.org/rfc/rfc4791.txt
    """
    def _create(self, name, id=None, supported_calendar_component_set=None):
        """
        Create a new calendar with display name `name` in `parent`.
        """
        if id is None:
            id = str(uuid.uuid1())
        self.id = id

        path = self.parent.url.join(id)
        self.url = path

        ## TODO: mkcalendar seems to ignore the body on most servers?  
        ## at least the name doesn't get set this way.
        ## zimbra gives 500 (!) if body is omitted ...

        cal = cdav.CalendarCollection()
        coll = dav.Collection() + cal
        type = dav.ResourceType() + coll

        prop = dav.Prop() + [type,]
        if name:
            display_name = dav.DisplayName(name)
            prop += [display_name,]
        if supported_calendar_component_set:
            sccs = cdav.SupportedCalendarComponentSet()
            for scc in supported_calendar_component_set:
                sccs += cdav.Comp(scc)
            prop += sccs
        set = dav.Set() + prop

        mkcol = cdav.Mkcalendar() + set

        r = self._query(root=mkcol, query_method='mkcalendar', url=path, expected_return_value=201)

        if name:
            try:
                self.set_properties([display_name])
            except:
                self.delete()
                raise

        ## Special hack for Zimbra!  The calendar we've made exists at
        ## the specified URL, and we can do operations like ls, even
        ## PUT an event to the calendar.  Zimbra will enforce that the
        ## event uuid matches the event url, and return either 201 or
        ## 302 - but alas, try to do a GET towards the event and we
        ## get 404!  But turn around and replace the calendar ID with
        ## the calendar name in the URL and hey ... it works!  

        ## TODO: write test cases for calendars with non-trivial
        ## names and calendars with names already matching existing
        ## calendar urls and ensure they pass.
        zimbra_url = self.parent.url.join(name)
        try:
            ret = self.client.request(zimbra_url)
            if ret.status == 404:
                raise error.NotFoundError
            ## insane server
            self.url = zimbra_url
        except error.NotFoundError:
            ## sane server
            pass
        
    def add_event(self, ical):
        return Event(self.client, data = ical, parent = self).save()

    def add_todo(self, ical):
        return Todo(self.client, data = ical, parent = self).save()

    def save(self):
        """
        The save method for a calendar is only used to create it, for now.
        We know we have to create it when we don't have a url.

        Returns:
         * self
        """
        if self.url is None:
            self._create(name=self.name, id=self.id, **self.extra_init_options)
            if not self.url.endswith('/'):
                self.url = URL.objectify(str(self.url) + '/')
        return self

    def date_search(self, start, end=None):
        """
        Search events by date in the calendar. Recurring events are expanded
        if they have an occurence during the specified time frame.

        Parameters:
         * start = datetime.today().
         * end = same as above.

        Returns:
         * [Event(), ...]
        """
        matches = []

        ## TODO: quite much overlapping with _query_properties, consider some refactoring
        
        # build the request
        expand = cdav.Expand(start, end)
        data = cdav.CalendarData() + expand
        prop = dav.Prop() + data

        range = cdav.TimeRange(start, end)
        vevent = cdav.CompFilter("VEVENT") + range
        vcalendar = cdav.CompFilter("VCALENDAR") + vevent
        filter = cdav.Filter() + vcalendar

        root = cdav.CalendarQuery() + [prop, filter]

        q = etree.tostring(root.xmlelement(), encoding="utf-8",
                           xml_declaration=True)
        response = self.client.report(self.url, q, 1)
        results = self._handle_prop_response(response=response, props=[cdav.CalendarData()])
        for r in results:
            matches.append(
                Event(self.client, url=self.url.join(r), data=results[r][cdav.CalendarData.tag], parent=self))
        
        return matches

    def todos(self, sort_key='DUE'):
        """
        fetches a list of todo events.
        """
        ## ref https://www.ietf.org/rfc/rfc4791.txt, section 7.8.9
        matches = []

        ## TODO: quite much overlapping with _query_properties, consider some refactoring
        
        # build the request
        data = cdav.CalendarData()
        prop = dav.Prop() + data

        vnotcompleted = cdav.TextMatch('COMPLETED', negate=True)
        vnotcancelled = cdav.TextMatch('CANCELLED', negate=True)
        vstatus = cdav.PropFilter('STATUS') + vnotcancelled + vnotcompleted
        vnocompletedate = cdav.PropFilter('COMPLETED') + cdav.NotDefined()
        vtodo = cdav.CompFilter("VTODO") + vnocompletedate + vstatus
        vcalendar = cdav.CompFilter("VCALENDAR") + vtodo
        filter = cdav.Filter() + vcalendar

        root = cdav.CalendarQuery() + [prop, filter]

        q = etree.tostring(root.xmlelement(), encoding="utf-8",
                           xml_declaration=True)
        
        response = self.client.report(self.url, q, 1)
        results = self._handle_prop_response(response=response, props=[cdav.CalendarData()])
        for r in results:
            matches.append(
                Todo(self.client, url=self.url.join(r), data=results[r][cdav.CalendarData.tag], parent=self))
        
        return matches

    def event_by_url(self, href, data=None):
        return Event(url=href, data=data, parent=self).load()

    def event_by_uid(self, uid):
        """
        Get one event from the calendar.

        Parameters:
         * uid: the event uid

        Returns:
         * Event() or None
        """
        data = cdav.CalendarData()
        prop = dav.Prop() + data

        match = cdav.TextMatch(uid)
        propf = cdav.PropFilter("UID") + match
        vevent = cdav.CompFilter("VEVENT") + propf
        vcalendar = cdav.CompFilter("VCALENDAR") + vevent
        filter = cdav.Filter() + vcalendar

        root = cdav.CalendarQuery() + [prop, filter]

        response = self._query(root, 1, 'report')

        if response.status == 404:
            raise error.NotFoundError(response.raw)
        elif response.status == 400:
            raise error.ReportError(response.raw)
            
        r = response.tree.find(".//" + dav.Response.tag)
        if r is not None:
            href = r.find(".//" + dav.Href.tag).text
            data = r.find(".//" + cdav.CalendarData.tag).text
            return Event(self.client, url=URL.objectify(href), data=data, parent=self)
        else:
            raise error.NotFoundError(response.raw)

    ## alias for backward compatibility
    event = event_by_uid

    def events(self):
        """
        List all events from the calendar.  TODO: a bit assymmetric with
        the "todos"-method, as this method probably will return
        todo-items as well.

        Returns:
         * [Event(), ...]

        """
        all = []

        ## TODO: do the filtering on the server side
        data = self.children()
        for e_url, e_type in data:
            all.append(Event(self.client, e_url, parent=self))

        return all

class CalendarObjectResource(DAVObject):
    """
    Ref RFC 4791, section 4.1, a "Calendar Object Resource" can be an
    event, a todo-item, a journal entry, a free/busy entry, etc.
    """
    _instance = None
    _data = None

    def __init__(self, client=None, url=None, data=None, parent=None, id=None):
        """
        CalendarObjectResource has an additional parameter for its constructor:
         * data = "...", vCal data for the event
        """
        DAVObject.__init__(self, client=client, url=url, parent=parent, id=id)
        if data is not None:
            self.data = data

    def load(self):
        """
        Load the object from the caldav server.
        """
        r = self.client.request(self.url)
        if r.status == 404:
            raise error.NotFoundError(r.raw)
        self.data = vcal.fix(r.raw)
        return self

    def _create(self, data, id=None, path=None):
        if id is None and path is not None and str(path).endswith('.ics'):
            id = re.search('(/|^)([^/]*).ics',str(path)).group(2)
        elif id is None:
            for obj in ('vevent', 'vtodo', 'vjournal', 'vfreebusy'):
                if hasattr(self.instance, obj):
                    id = getattr(self.instance, obj).uid.value
                    break
        if path is None:
            path = id + ".ics"
        path = self.parent.url.join(path)
        r = self.client.put(path, data,
                            {"Content-Type": 'text/calendar; charset="utf-8"'})

        if r.status == 302:
            path = [x[1] for x in r.headers if x[0]=='location'][0]
        elif not (r.status in (204, 201)):
            raise error.PutError(r.raw)

        self.url = URL.objectify(path)
        self.id = id

    def save(self):
        """
        Save the object, can be used for creation and update.

        Returns:
         * self
        """
        if self._instance is not None:
            path = self.url.path if self.url else None
            self._create(self._instance.serialize(), self.id, path)
        return self

    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, self.url)

    def set_data(self, data):
        self._data = vcal.fix(data)
        self._instance = vobject.readOne(io.StringIO(to_unicode(self._data)))
        return self

    def get_data(self):
        return self._data
    data = property(get_data, set_data,
                    doc="vCal representation of the object")

    def set_instance(self, inst):
        self._instance = inst
        self._data = inst.serialize()
        return self

    def get_instance(self):
        return self._instance
    instance = property(get_instance, set_instance,
                        doc="vobject instance of the object")


class Event(CalendarObjectResource):
    """
    The `Event` object is used to represent an event (VEVENT).
    """
    pass
    
class Todo(CalendarObjectResource):
    """
    The `Todo` object is used to represent a todo item (VTODO).
    """
    pass
