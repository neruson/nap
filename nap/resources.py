import requests

from .lookup import default_lookup_urls
from .utils import make_url
from .fields import Field
from .serializers import JSONSerializer


class DataModelMetaClass(type):

    def __new__(cls, name, bases, attrs):
        super_new = super(DataModelMetaClass, cls).__new__
        parents = [b for b in bases if isinstance(b, DataModelMetaClass)]
        if not parents:
            return super_new(cls, name, bases, attrs)

        model_cls = super_new(cls, name, bases, attrs)
        fields = {}

        options = attrs.pop('Meta', None)
        default_name = model_cls.__name__.lower()
        resource_name = getattr(options, 'resource_name', default_name)

        urls = getattr(options, 'urls', default_lookup_urls)
        additional_urls = tuple(getattr(options, 'additional_urls', ()))
        urls += additional_urls

        _meta = {
            'resource_name': resource_name,
            'root_url': getattr(options, 'root_url', None),
            'urls': urls,
            'resource_id_field_name': None,
            'add_slash': getattr(options, 'add_slash', True),
        }

        for name, attr in attrs.iteritems():
            if isinstance(attr, Field):
                attr._name = name
                fields[name] = attr
                setattr(model_cls, name, attr)

                if attr.resource_id:
                    _meta['resource_id_field_name'] = name

        _meta['fields'] = fields
        setattr(model_cls, '_meta', _meta)
        return model_cls


class ResourceModel(object):

    __metaclass__ = DataModelMetaClass

    def __init__(self, *args, **kwargs):
        self._root_url = kwargs.get('root_url', self._meta['root_url'])
        self._saved = False
        self.update_fields(kwargs)

    def update_fields(self, field_data):

        model_fields = self._meta['fields']
        api_name_map = dict([
            (field.api_name or name, name)
            for (name, field) in model_fields.iteritems()
        ])

        extra_data = set(field_data.keys()) - set(api_name_map.keys())
        for api_name, field_name in api_name_map.iteritems():
            model_field = model_fields[field_name]

            if api_name in field_data:
                value = model_field.scrub_value(field_data[api_name])
            else:
                value = model_field.get_default()

            setattr(self, field_name, value)

        self.extra_data = dict([
            (key, field_data[key])
            for key in extra_data
        ])

    def _generate_url(self, url_type='lookup', **kwargs):
        valid_urls = [
            url for url in self._meta['urls']
            if getattr(url, url_type, False)
        ]
        for url in valid_urls:
            if isinstance(self, ResourceModel):
                base_vars = dict([
                    (var, getattr(self, var))
                    for var in url.required_vars
                    if getattr(self, var, None)
                ])
            else:
                base_vars = {}

            if self._resource_id_name in kwargs and 'resource_id' in url.required_vars:
                kwargs['resource_id'] = kwargs[self._resource_id_name]
                if self._resource_id_name not in url.required_vars:
                    del kwargs[self._resource_id_name]

            base_vars.update(kwargs)
            model_keywords = {
                'resource_name': self._meta['resource_name']
            }

            url_model_keywords = dict([
                (k, v) for (k, v) in model_keywords.items()
                if k in url.url_parts
            ])
            url_model_keywords.update(base_vars)

            base_uri, params = url.match(**url_model_keywords)

            if base_uri:
                full_uri = make_url(base_uri,
                    params=params,
                    add_slash=self._meta['add_slash'])

                return full_uri

        raise ValueError("No valid url")

    def _request(self, url, request_func, *args, **kwargs):
        try:
            root_url = self._meta['root_url']
        except KeyError:
            raise ValueError("Nap requests require root_url to be defined")
        full_url = "%s%s" % (root_url, url)

        resource_response = request_func(full_url, *args, **kwargs)

        return resource_response

    # url methods
    @classmethod
    def get_lookup_url(cls, **kwargs):
        self = cls()
        return self._generate_url(**kwargs)

        raise ValueError("no valid URL for lookup found")

    def get_update_url(self, **kwargs):
        if self.full_url:
            return self.full_url

        try:
            update_uri = self._generate_url(url_type='update', **kwargs)
        except ValueError:
            update_uri = None

        return update_uri

    def get_create_url(self, **kwargs):
        return self._generate_url(url_type='create', **kwargs)

    # access methods
    @classmethod
    def get_from_uri(cls, uri, *args, **kwargs):
        self = cls(**kwargs)

        resource_response = self._request(uri, requests.get, *args, **kwargs)
        resource_data = self.deserialize(resource_response.content)

        self.update_fields(resource_data)
        self._full_url = resource_response.url

        return self

    @classmethod
    def get(cls, uri=None, **kwargs):

        if uri:
            return cls.get_from_uri(uri)

        return cls.lookup(**kwargs)

    @classmethod
    def lookup(cls, **kwargs):
        uri = cls.get_lookup_url(**kwargs)
        return cls.get(uri)

    def update(self, **kwargs):
        headers = {'content-type': 'application/json'}

        url = self.get_update_url()
        if not url:
            raise ValueError('No update url found')

        r = self._request(url, requests.put,
            data=self.serialize(),
            headers=headers)

        if r.status_code == 204:
            self._full_url = url

        self.handle_update_response(r)

    def handle_update_response(self, r):
        pass

    def create(self, **kwargs):
        headers = {'content-type': 'application/json'}

        r = self._request(self.get_create_url(), requests.post,
            data=self.serialize(),
            headers=headers)

        if r.status_code == 201:
            full_url = r.headers.get('location', None)
            self._full_url = full_url.replace(self._root_url, '')

        self.handle_create_response(r)

    def handle_create_response(self, r):
        pass

    # write methods
    def save(self, **kwargs):
        # this feels off to me, but it should work for now?
        if self._saved or self.full_url or self.get_update_url():
            self.update(**kwargs)
        else:
            self.create(**kwargs)

    # utility methods
    def to_python(self):
        obj_dict = dict([
            (field_name, field.descrub_value(getattr(self, field_name)))
            for field_name, field in self._meta['fields'].iteritems()
        ])

        return obj_dict

    def serialize(self):
        serializer = self.get_serializer()
        return serializer.serialize(self.to_python())

    def deserialize(self, val_str):
        serializer = self.get_serializer()
        obj_dict = serializer.deserialize(val_str)

        return obj_dict

    def get_serializer(self):
        return JSONSerializer()

    # properties
    @property
    def full_url(self):
        return getattr(self, '_full_url', None)

    @property
    def _resource_id_name(self):
        if not self._meta['resource_id_field_name']:
            return None
        id_field_name = self._meta['resource_id_field_name']
        return id_field_name

    @property
    def resource_id(self):
        if not self._resource_id_name:
            return None

        return getattr(self, self._resource_id_name)

    @resource_id.setter
    def resource_id(self, resource_id_value):
        if not self._resource_id_name:
            return None
        setattr(self, self._resource_id_name, resource_id_value)

    # etc
    def __unicode__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.resource_id)

    def __repr__(self):
        return unicode(self)
