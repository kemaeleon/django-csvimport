""" Developed for www.heliosfoundation.org by Ed Crewe and Tom Dunham 
    Django command to import CSV files
"""
import os, csv, re
from datetime import datetime
import codecs
import chardet
#FIXMEs name clash between management command and module
from ...signals import imported_csv, importing_csv

from django.db import DatabaseError
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import LabelCommand, BaseCommand
from optparse import make_option
from django.db import models
from django.contrib.contenttypes.models import ContentType

from django.conf import settings
CSVIMPORT_LOG = getattr(settings, 'CSVIMPORT_LOG', 'screen')
if CSVIMPORT_LOG == 'logger':
    import logging
    logger = logging.getLogger(__name__)

INTEGER = ['BigIntegerField', 'IntegerField', 'AutoField',
           'PositiveIntegerField', 'PositiveSmallIntegerField']
FLOAT = ['DecimalField', 'FloatField']
NUMERIC = INTEGER + FLOAT
DATE = ['DateField', 'TimeField', 'DateTimeField']
BOOLEAN = ['BooleanField', 'NullBooleanField']
BOOLEAN_TRUE = [1, '1', 'Y', 'Yes', 'yes', 'True', 'true', 'T', 't']
DATE_INPUT_FORMATS = settings.DATE_INPUT_FORMATS or ('%d/%m/%Y','%Y/%m/%d')
CSV_DATE_INPUT_FORMATS = DATE_INPUT_FORMATS + ('%d-%m-%Y','%Y-%m-%d')
cleancol = re.compile('[^0-9a-zA-Z]+')  # cleancol.sub('_', s)

# Note if mappings are manually specified they are of the following form ...
# MAPPINGS = "column1=shared_code,column2=org(Organisation|name),column3=description"
# statements = re.compile(r";[ \t]*$", re.M)

def save_csvimport(props=None, instance=None):
    """ To avoid circular imports do saves here """
    try:
        if not instance:
            from csvimport.models import CSVImport
            csvimp = CSVImport()
        if props:
            for key, value in props.items():
                setattr(csvimp, key, value)
        csvimp.save()
        return csvimp.id
    except:
        # Running as command line
        print 'Assumed charset = %s\n' % instance.charset
        print '###############################\n'
        for line in instance.loglist:
            if type(line) != type(''):
                for subline in line:
                    print subline
                    print
            else:
                print line
                print

class Command(LabelCommand):
    """
    Parse and import a CSV resource to a Django model.

    Notice that the doc tests are merely illustrational, and will not run
    as is.
    """

    option_list = BaseCommand.option_list + (
               make_option('--mappings', default='',
                           help='''Provide comma separated column names or format like
                                   (column1=field1(ForeignKey|field),column2=field2(ForeignKey|field), ...)
                                   for the import (use none for no names -> col_#)'''),
               make_option('--defaults', default='',
                           help='''Provide comma separated defaults for the import 
                                   (field1=value,field3=value, ...)'''),
               make_option('--model', default='iisharing.Item',
                           help='Please provide the model to import to'),
               make_option('--charset', default='',
                           help='Force the charset conversion used rather than detect it')
                   )
    help = "Imports a CSV file to a model"


    def __init__(self):
        """ Set default attributes data types """
        super(Command, self).__init__()
        self.props = {}
        self.debug = False
        self.errors = []
        self.loglist = []
        self.mappings = []
        self.defaults = []
        self.app_label = ''
        self.model = ''
        self.fieldmap = {}
        self.file_name = ''
        self.nameindexes = False
        self.deduplicate = False
        self.csvfile = []
        self.charset = ''
        self.filehandle = None
        self.makemodel = ''
        self.start = 1

    def handle_label(self, label, **options):
        """ Handle the circular reference by passing the nested
            save_csvimport function
        """
        filename = label
        mappings = options.get('mappings', [])
        defaults = options.get('defaults', [])
        modelname = options.get('model', 'Item')
        charset = options.get('charset', '')
        # show_traceback = options.get('traceback', True)
        self.setup(mappings, modelname, charset, filename, defaults)
        if not hasattr(self.model, '_meta'):
            msg = 'Sorry your model could not be found please check app_label.modelname'
            try:
                print msg
            except:
                self.loglist.append(msg)
            return
        errors = self.run()
        if self.props:
            save_csvimport(self.props, self)
        self.loglist.extend(errors)
        return

    def setup(self, mappings, modelname, charset, csvfile='', defaults='',
              uploaded=None, nameindexes=False, deduplicate=False):
        """ Setup up the attributes for running the import """
        self.defaults = self.__mappings(defaults)
        if modelname.find('.') > -1:
            app_label, model = modelname.split('.')
        if uploaded:
            self.csvfile = self.__csvfile(uploaded.path)
        else:
            self.check_filesystem(csvfile)
        if app_label == 'create_new_model':
            app_label = 'csvimport'
            self.makemodel = 'Please create a model as below then rerun the csv import'
            self.makemodel += '(TODO: Write this to a models.py, syncdb, and do import all at once?)'
            self.makemodel += self.create_new_model(model)
            self.loglist.append(self.makemodel)
            print self.makemodel
            return
        self.charset = charset
        self.app_label = app_label
        self.model = models.get_model(app_label, model)
        for field in self.model._meta.fields:
            self.fieldmap[field.name] = field
            if field.__class__ == models.ForeignKey:
                self.fieldmap[field.name+"_id"] = field
        if mappings:
            if mappings == 'none':
                # Use auto numbered cols instead - eg. from create_new_model
                mappings = self.parse_header(['col_%s' % num for num in range(1, len(self.csvfile[0]))])
            # Test for column=name or just name list format
            if mappings.find('=') == -1:
                mappings = self.parse_header(mappings.split(','))
            self.mappings = self.__mappings(mappings)
        self.nameindexes = bool(nameindexes)
        self.file_name = csvfile
        self.deduplicate = deduplicate
        return 

    def create_new_model(self, modelname):
        """ Use messytables to guess field types and build a new model """

        nocols = False
        cols = self.csvfile[0]
        for col in cols:
            if not col:
                nocols = True
        if nocols:
            cols = ['col_%s' % num for num in range(1, len(cols))]
            print 'No column names for %s columns' % len(cols)
        else:
            cols = [cleancol.sub('_', col).lower() for col in cols]
        try:
            from messytables import any_tableset, type_guess
        except:
            self.errors.append('If you want to create tables, you must install https://messytables.readthedocs.org')
            self.modelname = ''
            return
        try:
            table_set = any_tableset(self.filehandle)
            row_set = table_set.tables[0]
            types = type_guess(row_set.sample)
            types = [str(typeobj) for typeobj in types]
        except:
            self.errors.append('messytables could not guess your column types')
            self.modelname = ''
            return

        print 'Done type scanning'
        fieldset = []
        maximums = self.get_maxlengths(cols)
        for i, col in enumerate(cols):
            length = maximums[i]
            if types[i] == 'String' and length>255:
                types[i] = 'Text'
            integer = length
            decimal = int(length/2)
            if decimal > 10:
                decimal = 10
            blank = True
            default = True
            column = (col, types[i], length, length, integer, decimal, blank, default)
            fieldset.append(column)
        print 'Done column setup'
        from ...make_model import MakeModel
        maker = MakeModel()
        return maker.model_from_table(modelname, fieldset)

    def get_maxlengths(self, cols):
        """ Get maximum column length values to avoid truncation 
            -- can always manually reduce size of fields after auto model creation
        """
        maximums = [0]*len(cols)
        for line in self.csvfile[1:100]:
            for i, value in enumerate(line):
                if value and len(value) > maximums[i]:
                    maximums[i] = len(value)
                if maximums[i] > 10:
                    maximums[i] += 10
                if not maximums[i]:
                    maximums[i] = 10
        return maximums

    def check_fkey(self, key, field):
        """ Build fkey mapping via introspection of models """
        #TODO fix to find related field name rather than assume second field
        if not key.endswith('_id'):
            if field.__class__ == models.ForeignKey:
                key += '(%s|%s)' % (field.related.parent_model.__name__,
                                    field.related.parent_model._meta.fields[1].name,)
        return key

    def check_filesystem(self, csvfile):
        """ Check for files on the file system """
        if os.path.exists(csvfile):
            if os.path.isdir(csvfile):
                self.csvfile = []
                for afile in os.listdir(csvfile):
                    if afile.endswith('.csv'):
                        filepath = os.path.join(csvfile, afile)
                        try:
                            lines = self.__csvfile(filepath)
                            self.csvfile.extend(lines)
                        except:
                            pass
            else:
                self.csvfile = self.__csvfile(csvfile)
        if not getattr(self, 'csvfile', []):
            raise Exception('File %s not found' % csvfile)

    def run(self, logid=0):
        """ Run the csvimport """
        loglist = []
        if self.nameindexes:
            indexes = self.csvfile.pop(0)
        counter = 0
        if logid:
            csvimportid = logid
        else:
            csvimportid = 0

        if self.mappings:
            self.start = 0
            loglist.append('Using manually entered (or default) mapping list')
        else:
            mappingstr = self.parse_header(self.csvfile[0])
            if mappingstr:
                loglist.append('Using mapping from first row of CSV file')
                self.mappings = self.__mappings(mappingstr)
        if not self.mappings:
            if not self.model:
                loglist.append('Outputting setup message')
            else:
                loglist.append('''No fields in the CSV file match %s.%s\n
                                   - you must add a header field name row
                                   to the CSV file or supply a mapping list''' %
                                (self.model._meta.app_label, self.model.__name__))
            return loglist

        rowcount = 0
        for i, row in enumerate(self.csvfile[self.start:]):
            if CSVIMPORT_LOG == 'logger':
                logger.info("Import %s %i", self.model.__name__, counter)
            counter += 1

            model_instance = self.model()
            model_instance.csvimport_id = csvimportid

            for (column, field, foreignkey) in self.mappings:
                if self.nameindexes:
                    column = indexes.index(column)
                else:
                    column = int(column)-1

                if foreignkey:
                    row[column] = self.insert_fkey(foreignkey, row[column])

                if self.debug:
                    loglist.append('%s.%s = "%s"' % (self.model.__name__,
                                                          field, row[column]))

                row[column] = self.type_clean(field, row[column], loglist, i)

                try:
                    model_instance.__setattr__(field, row[column])
                except:
                    try:
                        value = model_instance.getattr(field).to_python(row[column])
                    except:
                        msg = 'row %s: Column %s = %s couldnt be set for row' % (i, field, row[column])
                        loglist.append(msg)


            if self.defaults:
                for (field, value, foreignkey) in self.defaults:
                    value = self.type_clean(field, value, loglist)
                    try:
                        done = model_instance.getattr(field)
                    except:
                        done = False
                    if not done:
                        if foreignkey:
                            value = self.insert_fkey(foreignkey, value)
                    if value:
                        model_instance.__setattr__(field, value)

            if self.deduplicate:
                matchdict = {}
                for (column, field, foreignkey) in self.mappings:
                    matchdict[field + '__exact'] = getattr(model_instance,
                                                           field, None)
                try:
                    self.model.objects.get(**matchdict)
                    continue
                except:
                    pass
            try:
                importing_csv.send(sender=model_instance,
                                   row=dict(zip(self.csvfile[:1][0], row)))
                model_instance.save()
                imported_csv.send(sender=model_instance,
                                  row=dict(zip(self.csvfile[:1][0], row)))
                rowcount += 1
            except DatabaseError, err:
                try:
                    error_number, error_message = err
                except:
                    error_message = err
                    error_number = 0
                # Catch duplicate key error.
                if error_number != 1062:
                    loglist.append(
                        'Database Error: %s, Number: %d' % (error_message,
                                                            error_number))
            #except OverflowError:
            #    pass

            if CSVIMPORT_LOG == 'logger':
                for line in loglist:
                    logger.info(line)
            self.loglist.append(loglist)
            loglist = []
        countmsg = 'Imported %s rows to %s' % (rowcount, self.model.__name__)
        if CSVIMPORT_LOG == 'logger':
            logger.info(countmsg)            
        if self.loglist:
            self.loglist.append(countmsg)
            self.props = {'file_name':self.file_name,
                          'import_user':'cron',
                          'upload_method':'cronjob',
                          'error_log':'\n'.join(loglist),
                          'import_date':datetime.now()}
            return self.loglist
        else:
            return ['No logging', ]

    def type_clean(self, field, value, loglist, row=0):
        """ Data value clean up - type formatting"""
        field_type = self.fieldmap.get(field).get_internal_type()

        try:
            value = value.strip()
        except AttributeError:
            pass

        # Tidy up boolean data
        if field_type in BOOLEAN:
            value = value in BOOLEAN_TRUE

        # Tidy up numeric data
        if field_type in NUMERIC:
            if not value:
                value = 0
            else:
                try:
                    value = float(value)
                except:
                    loglist.append('row %s: Column %s = %s is not a number so is set to 0' \
                                        % (row, field, value))
                    value = 0
            if field_type in INTEGER:
                if value > 9223372036854775807:
                    loglist.append('row %s: Column %s = %s more than the max integer 9223372036854775807' \
                                        % (row, field, value))
                if str(value).lower() in ('nan', 'inf', '+inf', '-inf'):
                    loglist.append('row %s: Column %s = %s is not an integer so is set to 0' \
                                        % (row, field, value))
                    value = 0
                value = int(value)
                if value < 0 and field_type.startswith('Positive'):
                    loglist.append('row %s: Column %s = %s, less than zero so set to 0' \
                                        % (row, field, value))
                    value = 0
        # date data - remove the date if it doesn't convert so null=True can work
        if field_type in DATE:
            datevalue = None
            try:
                datevalue = datetime(value)
            except:
                for datefmt in CSV_DATE_INPUT_FORMATS:
                    try:
                        datevalue = datetime.strptime(value, datefmt)
                    except:
                        pass

            if datevalue:
                value = datevalue
            else:
                # loglist.append('row %s: Column %s = %s not date format' % (i, field, value))
                value = None
        return value

    def parse_header(self, headlist):
        """ Parse the list of headings and match with self.fieldmap """
        mapping = []
        headlist = [cleancol.sub('_', col) for col in headlist]
        self.loglist.append('Columns = %s' % str(headlist)[1:-1])
        for i, heading in enumerate(headlist):
            for key in ((heading, heading.lower(),
                         ) if heading != heading.lower() else (heading,)):
                if self.fieldmap.has_key(key):
                    field = self.fieldmap[key]
                    key = self.check_fkey(key, field)
                    mapping.append('column%s=%s' % (i+1, key))
        if mapping:
            return ','.join(mapping)
        return ''

    def insert_fkey(self, foreignkey, rowcol):
        """ Add fkey if not present
            If there is corresponding data in the model already,
            we do not need to add more, since we are dealing with
            foreign keys, therefore foreign data
        """
        fk_key, fk_field = foreignkey
        if fk_key and fk_field:
            try:
                new_app_label = ContentType.objects.get(model=fk_key).app_label
            except:
                new_app_label = self.app_label
            fk_model = models.get_model(new_app_label, fk_key)
            matches = fk_model.objects.filter(**{fk_field+'__exact':
                                                 rowcol})

            if not matches:
                key = fk_model()
                key.__setattr__(fk_field, rowcol)
                key.save()

            rowcol = fk_model.objects.filter(**{fk_field+'__exact': rowcol})[0]
        return rowcol

    def error(self, message, type=1):
        """
        Types:
            0. A fatal error. The most drastic one. Will quit the program.
            1. A notice. Some minor thing is in disorder.
        """

        types = (
            ('Fatal error', FatalError),
            ('Notice', None),
        )

        self.errors.append((message, type))

        if type == 0:
            # There is nothing to do. We have to quit at this point
            raise types[0][1], message
        elif self.debug == True:
            print "%s: %s" % (types[type][0], message)

    def __csvfile(self, datafile):
        """ Detect file encoding and open appropriately """
        self.filehandle = open(datafile)
        if not self.charset:
            diagnose = chardet.detect(self.filehandle.read())
            self.charset = diagnose['encoding']
        try:
            csvfile = codecs.open(datafile, 'r', self.charset)
        except IOError:
            self.error('Could not open specified csv file, %s, or it does not exist' % datafile, 0)
        else:
            # CSV Reader returns an iterable, but as we possibly need to
            # perform list commands and since list is an acceptable iterable,
            # we'll just transform it.
            try:
                return list(self.charset_csv_reader(csv_data=csvfile,
                                                charset=self.charset))
            except:
                output = []
                count = 0
                # Sometimes encoding is too mashed to be able to open the file as text
                # so reopen as raw unencoded and just try and get lines out one by one
                # Assumes "," \r\n delimiters
                try:
                    with open(datafile, 'rb') as content_file:
                        content = content_file.read()
                    if content:
                        rows = content.split('\r\n')
                        for row in rows:
                            rowlist = row[1:-1].split('","')
                            if row:
                                count += 1
                                try:
                                    output.append(rowlist)
                                except:
                                    self.loglist.append('Failed to parse row %s' % count)
                except:
                    self.loglist.append('Failed to open file %s' % datafile)
                return output

    def charset_csv_reader(self, csv_data, dialect=csv.excel,
                           charset='utf-8', **kwargs):
        csv_reader = csv.reader(self.charset_encoder(csv_data, charset),
                                dialect=dialect, **kwargs)
        for row in csv_reader:
            # decode charset back to Unicode, cell by cell:
            yield [unicode(cell, charset) for cell in row]

    def charset_encoder(self, csv_data, charset='utf-8'):
        """ Check passed a valid charset then encode """
        test_string = 'test_real_charset'
        try:
            test_string.encode(charset)
        except:
            charset = 'utf-8'
        for line in csv_data:
            yield line.encode(charset)

    def __mappings(self, mappings):
        """
        Parse the mappings, and return a list of them.
        """
        if not mappings:
            return []

        def parse_mapping(args):
            """
            Parse the custom mapping syntax (column1=field1(ForeignKey|field),
            etc.)

            >>> parse_mapping('a=b(c|d)')
            [('a', 'b', '(c|d)')]
            """
            # value = word or date format match
            pattern = re.compile(r'(\w+)=(\d+/\d+/\d+|\d+-\d+-\d+|\w+)(\(\w+\|\w+\))?')
            mappings = pattern.findall(args)

            mappings = list(mappings)
            for mapping in mappings:
                mapp = mappings.index(mapping)
                mappings[mapp] = list(mappings[mapp])
                mappings[mapp][2] = parse_foreignkey(mapping[2])
                mappings[mapp] = tuple(mappings[mapp])
            mappings = list(mappings)
            
            return mappings

        def parse_foreignkey(key):
            """
            Parse the foreignkey syntax (Key|field)

            >>> parse_foreignkey('(a|b)')
            ('a', 'b')
            """

            pattern = re.compile(r'(\w+)\|(\w+)', re.U)
            if key.startswith('(') and key.endswith(')'):
                key = key[1:-1]

            found = pattern.search(key)

            if found != None:
                return (found.group(1), found.group(2))
            else:
                return None

        mappings = mappings.replace(',', ' ')
        mappings = mappings.replace('column', '')
        return parse_mapping(mappings)


class FatalError(Exception):
    """
    Something really bad happened.
    """
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)

