import abc
import logging

from .fields import EmailSubField, LabelField, SubField, NamedSubField, Choice
from .properties import EWSElement, Fields

log = logging.getLogger(__name__)


class IndexedElement(EWSElement, metaclass=abc.ABCMeta):
    """Base class for all classes that implement an indexed element"""
    LABELS = set()

    __slots__ = tuple()


class SingleFieldIndexedElement(IndexedElement, metaclass=abc.ABCMeta):
    """Base class for all classes that implement an indexed element with a single field"""
    __slots__ = tuple()

    @classmethod
    def value_field(cls, version=None):
        fields = cls.supported_fields(version=version)
        if len(fields) != 1:
            raise ValueError('This class must have only one field (found %s)' % (fields,))
        return fields[0]


class EmailAddress(SingleFieldIndexedElement):
    """MSDN: https://docs.microsoft.com/en-us/exchange/client-developer/web-service-reference/entry-emailaddress"""
    ELEMENT_NAME = 'Entry'
    LABEL_CHOICES = ('EmailAddress1', 'EmailAddress2', 'EmailAddress3')
    FIELDS = Fields(
        LabelField('label', field_uri='Key', choices={Choice(c) for c in LABEL_CHOICES}, default=LABEL_CHOICES[0]),
        EmailSubField('email'),
    )

    __slots__ = tuple(f.name for f in FIELDS)


class PhoneNumber(SingleFieldIndexedElement):
    """MSDN: https://docs.microsoft.com/en-us/exchange/client-developer/web-service-reference/entry-phonenumber"""
    ELEMENT_NAME = 'Entry'
    FIELDS = Fields(
        LabelField('label', field_uri='Key', choices={
            Choice('AssistantPhone'), Choice('BusinessFax'), Choice('BusinessPhone'), Choice('BusinessPhone2'),
            Choice('Callback'), Choice('CarPhone'), Choice('CompanyMainPhone'), Choice('HomeFax'), Choice('HomePhone'),
            Choice('HomePhone2'), Choice('Isdn'), Choice('MobilePhone'), Choice('OtherFax'), Choice('OtherTelephone'),
            Choice('Pager'), Choice('PrimaryPhone'), Choice('RadioPhone'), Choice('Telex'), Choice('TtyTddPhone'),
        }, default='PrimaryPhone'),
        SubField('phone_number'),
    )

    __slots__ = tuple(f.name for f in FIELDS)


class MultiFieldIndexedElement(IndexedElement, metaclass=abc.ABCMeta):
    """Base class for all classes that implement an indexed element with multiple fields"""
    __slots__ = tuple()


class PhysicalAddress(MultiFieldIndexedElement):
    """MSDN: https://docs.microsoft.com/en-us/exchange/client-developer/web-service-reference/entry-physicaladdress"""
    ELEMENT_NAME = 'Entry'
    FIELDS = Fields(
        LabelField('label', field_uri='Key', choices={
            Choice('Business'), Choice('Home'), Choice('Other')
        }, default='Business'),
        NamedSubField('street', field_uri='Street'),  # Street, house number, etc.
        NamedSubField('city', field_uri='City'),
        NamedSubField('state', field_uri='State'),
        NamedSubField('country', field_uri='CountryOrRegion'),
        NamedSubField('zipcode', field_uri='PostalCode'),
    )

    __slots__ = tuple(f.name for f in FIELDS)

    def clean(self, version=None):
        # pylint: disable=access-member-before-definition
        if isinstance(self.zipcode, int):
            self.zipcode = str(self.zipcode)
        super().clean(version=version)
