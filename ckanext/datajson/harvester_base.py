from ckan.lib.base import c
from ckan import model
from ckan import plugins as p
from ckan.model import Session, Package
from ckan.logic import ValidationError, NotFound, get_action
from ckan.lib.munge import munge_title_to_name
from ckan.lib.search.index import PackageSearchIndex
from ckan.lib.navl.dictization_functions import Invalid
from ckan.lib.navl.validators import ignore_empty

from ckanext.harvest.model import HarvestJob, HarvestObject, HarvestGatherError, \
                                    HarvestObjectError
from ckanext.harvest.harvesters.base import HarvesterBase

import uuid, datetime, hashlib, urllib2, json, yaml, json, os

from jsonschema.validators import Draft4Validator
from jsonschema import FormatChecker

import logging
log = logging.getLogger("harvester")

VALIDATION_SCHEMA = [
                        ('', 'Project Open Data (Federal)'),
                        ('non-federal', 'Project Open Data (Non-Federal)'),
                    ]

def validate_schema(schema):
    if schema not in [s[0] for s in VALIDATION_SCHEMA]:
        raise Invalid('Unknown validation schema: {0}'.format(schema))
    return schema

class DatasetHarvesterBase(HarvesterBase):
    '''
    A Harvester for datasets.
    '''
    _user_name = None

    # SUBCLASSES MUST IMPLEMENT
    #HARVESTER_VERSION = "1.0"
    #def info(self):
    #    return {
    #        'name': 'harvester_base',
    #        'title': 'Base Harvester',
    #        'description': 'Abstract base class for harvesters that pull in datasets.',
    #    }

    def validate_config(self, config):
        if not config:
            return config
        config_obj = yaml.load(config)
        return config

    def load_config(self, harvest_source):
        # Load the harvest source's configuration data. We expect it to be a YAML
        # string. Unfortunately I went ahead of CKAN on this. The stock CKAN harvester
        # only allows JSON in the configuration box. My fork is necessary for this
        # to work: https://github.com/joshdata/ckanext-harvest

        ret = {
            "filters": { }, # map data.json field name to list of values one of which must be present
            "defaults": { }, # map field name to value to supply as default if none exists, handled by the actual importer module, so the field names may be arbitrary
        }

        source_config = yaml.load(harvest_source.config)

        try:
            ret["filters"].update(source_config["filters"])
        except TypeError:
            pass
        except KeyError:
            pass

        try:
            ret["defaults"].update(source_config["defaults"])
        except TypeError:
            pass
        except KeyError:
            pass

        return ret

    def _get_user_name(self):
        if not self._user_name:
            user = p.toolkit.get_action('get_site_user')({'model': model, 'ignore_auth': True}, {})
            self._user_name = user['name']

        return self._user_name

    def context(self):
        # Reusing the dict across calls to action methods can be dangerous, so
        # create a new dict every time we need it.
        # Setting validate to False is critical for getting the harvester plugin
        # to set extra fields on the package during indexing (see ckanext/harvest/plugin.py
        # line 99, https://github.com/okfn/ckanext-harvest/blob/master/ckanext/harvest/plugin.py#L99).
        return { "user": self._get_user_name(), "ignore_auth": True }
        
    # SUBCLASSES MUST IMPLEMENT
    def load_remote_catalog(self, harvest_job):
        # Loads a remote data catalog. This function must return a JSON-able
        # list of dicts, each dict a dataset containing an 'identifier' field
        # with a locally unique identifier string and a 'title' field.
        raise Exception("Not implemented")

    def extra_schema(self):
        return {
            'validator_schema': [ignore_empty, unicode, validate_schema],
        }

    def gather_stage(self, harvest_job):
        # The gather stage scans a remote resource (like a /data.json file) for
        # a list of datasets to import.

        log.debug('In %s gather_stage (%s)' % (repr(self), harvest_job.source.url))

        # Start gathering.
        source = self.load_remote_catalog(harvest_job)
        if len(source) == 0: return []

        # Loop through the packages we've already imported from this source
        # and go into their extra fields to get their source_identifier,
        # which corresponds to the remote catalog's 'identifier' field.
        # Make a mapping so we know how to update existing records.
        existing_datasets = { }
        for hobj in model.Session.query(HarvestObject).filter_by(source=harvest_job.source, current=True):
            try:
                pkg = get_action('package_show')(self.context(), { "id": hobj.package_id })
            except:
                # reference is broken
                continue
            sid = self.find_extra(pkg, "identifier")
            if sid:
                existing_datasets[sid] = pkg
                    
        # Create HarvestObjects for any records in the remote catalog.
            
        object_ids = []
        seen_datasets = set()
        unique_datasets = set()
        
        filters = self.load_config(harvest_job.source)["filters"]

        for dataset in source:
            # Create a new HarvestObject for this dataset and save the
            # dataset metdata inside it for later.

            # Check the config's filters to see if we should import this dataset.
            # For each filter, check that the value specified in the data.json file
            # is among the permitted values in the filter specification.
            matched_filters = True
            for k, v in filters.items():
                if dataset.get(k) not in v:
                    matched_filters = False
            if not matched_filters:
                continue

            # Some source contains duplicate identifiers. skip all except the first one
            if dataset['identifier'] in unique_datasets:
                self._save_gather_error("Duplicate entry ignored for identifier: '%s'." % (dataset['identifier']), harvest_job)
                continue
            unique_datasets.add(dataset['identifier'])
            
            # Get the package_id of this resource if we've already imported
            # it into our system. Otherwise, assign a brand new GUID to the
            # HarvestObject. I'm not sure what the point is of that.
            
            if dataset['identifier'] in existing_datasets:
                pkg = existing_datasets[dataset["identifier"]]
                pkg_id = pkg["id"]
                seen_datasets.add(dataset['identifier'])
                
                # We store a hash of the dict associated with this dataset
                # in the package so we can avoid updating datasets that
                # don't look like they've changed.
                if pkg.get("state") == "active" \
                    and self.find_extra(pkg, "source_hash") == self.make_upstream_content_hash(dataset, harvest_job.source):
                    continue
            else:
                pkg_id = uuid.uuid4().hex

            # Create a new HarvestObject and store in it the GUID of the
            # existing dataset (if it exists here already) and the dataset's
            # metadata from the remote catalog file.
            obj = HarvestObject(
                guid=pkg_id,
                job=harvest_job,
                content=json.dumps(dataset, sort_keys=True)) # use sort_keys to preserve field order so hashes of this string are constant from run to run
            obj.save()
            object_ids.append(obj.id)
            
        # Remove packages no longer in the remote catalog.
        for upstreamid, pkg in existing_datasets.items():
            if upstreamid in seen_datasets: continue # was just updated
            if pkg.get("state") == "deleted": continue # already deleted
            pkg["state"] = "deleted"
            pkg["name"] = self.make_package_name(pkg["title"], pkg["id"], True) # try to prevent name clash by giving it a "deleted-" name
            log.warn('deleting package %s (%s) because it is no longer in %s' % (pkg["name"], pkg["id"], harvest_job.source.url))
            get_action('package_update')(self.context(), pkg)
            
        return object_ids

    def fetch_stage(self, harvest_object):
        # Nothing to do in this stage because we captured complete
        # dataset metadata from the first request to the remote catalog file.
        return True

    # SUBCLASSES MUST IMPLEMENT
    def set_dataset_info(self, pkg, dataset, dataset_defaults):
        # Sets package metadata on 'pkg' using the remote catalog's metadata
        # in 'dataset' and default values as configured in 'dataset_defaults'.
        raise Exception("Not implemented.")

    # validate dataset against POD schema
    # use a local copy.
    def _validate_dataset(self, validator_schema, dataset):
        if validator_schema == 'non-federal':
            json_file = open(os.path.join(os.path.dirname(__file__), 'pod_schema/non-federal/single_entry.json'))
        else:
            json_file = open(os.path.join(os.path.dirname(__file__), 'pod_schema/single_entry.json'))

        schema = json.load(json_file)
        errors = Draft4Validator(schema, format_checker=FormatChecker()).iter_errors(dataset)
        msg = ";"
        count = 0
        for error in errors:
            count += 1
            msg = msg + " ### ERROR #" + str(count) + ": " + self._validate_readable_msg(error) + "; "
        msg = msg.strip("; ")
        if msg:
            id = "Identifier: " + dataset.get("identifier", "Unknown")
            title = "Title: " + dataset.get("title", "Unknown")
            msg = id + "; " + title + "; " + str(count) + " Error(s) Found. " + msg + "."
        return msg

    # make ValidationError readable.
    def _validate_readable_msg(self, e):
        msg = e.message.replace("u'", "'")
        elem = ""
        try:
            if e.schema_path[0] == 'properties':
                elem = e.schema_path[1]
                elem = "'" + elem + "':" 
        except:
            pass

        return elem + msg

    def import_stage(self, harvest_object):
        # The import stage actually creates the dataset.
        
        log.debug('In %s import_stage' % repr(self))
        
        # Get default values.
        dataset_defaults = self.load_config(harvest_object.source)["defaults"]

        source_config = json.loads(harvest_object.source.config or '{}')
        validator_schema = source_config.get('validator_schema')
        if validator_schema == 'non-federal':
            lowercase_conversion = False
        else:
            lowercase_conversion = True

        # Get the metadata that we stored in the HarvestObject's content field.
        dataset = json.loads(harvest_object.content)

        MAPPING = {
            "title": "title",
            "description": "notes",
            "keyword": "tags",
            "modified": "extras__modified", # ! revision_timestamp
            "publisher": "extras__publisher", # !owner_org
            "contactPoint": "maintainer",
            "mbox": "maintainer_email",
            "identifier": "extras__identifier", # !id
            "accessLevel": "extras__accessLevel",

            "bureauCode": "extras__bureauCode",
            "programCode": "extras__programCode",
            "accessLevelComment": "extras__accessLevelComment",
            "license": "extras__license", # !license_id 
            "spatial": "extras__spatial", # Geometry not valid GeoJSON, not indexing
            "temporal": "extras__temporal",

            "theme": "extras__theme",
            "dataDictionary": "extras__dataDictionary", # !data_dict
            "dataQuality": "extras__dataQuality",
            "accrualPeriodicity":"extras__accrualPeriodicity",
            "landingPage": "extras__landingPage",
            "language": "extras__language",
            "primaryITInvestmentUII": "extras__primaryITInvestmentUII", # !PrimaryITInvestmentUII
            "references": "extras__references",
            "issued": "extras__issued",
            "systemOfRecords": "extras__systemOfRecords",

            "accessURL": None,
            "webService": None,
            "format": None,
            "distribution": None,
        }

        SKIP = ["accessURL", "webService", "format", "distribution"] # will go into pkg["resources"]
        # also skip the processed_how key, it was added to indicate how we processed the dataset.
        SKIP.append("processed_how");

        if lowercase_conversion:

            mapping_processed = {}
            for k,v in MAPPING.items():
                mapping_processed[k.lower()] = v

            skip_processed = [k.lower() for k in SKIP]

            dataset_processed = {'processed_how': ['lowercase']}
            for k,v in dataset.items():
              if k.lower() in mapping_processed.keys():
                dataset_processed[k.lower()] = v
              else:
                dataset_processed[k] = v

            if 'distribution' in dataset:
              dataset_processed['distribution'] = []
              for d in dataset['distribution']:
                d_lower = {}
                for k,v in d.items():
                  if k.lower() in mapping_processed.keys():
                    d_lower[k.lower()] = v
                  else:
                    d_lower[k] = v
                dataset_processed['distribution'].append(d_lower)
        else:
            dataset_processed = dataset
            mapping_processed = MAPPING
            skip_processed = SKIP

        validate_message = self._validate_dataset(validator_schema, dataset_processed)
        if validate_message:
            self._save_object_error(validate_message, harvest_object, 'Import')
            return None

        # We need to get the owner organization (if any) from the harvest
        # source dataset
        owner_org = None
        source_dataset = model.Package.get(harvest_object.source.id)
        if source_dataset.owner_org:
            owner_org = source_dataset.owner_org

        # Assemble basic information about the dataset.

        pkg = {
            "name": self.make_package_name(dataset_processed["title"], harvest_object.guid, False),
            "state": "active", # in case was previously deleted
            "owner_org": owner_org,
            "resources": [],
            "extras": [
                {
                    "key": "resource-type",
                    "value": "Dataset",
                },
                {
                    "key": "source_hash",
                    "value": self.make_upstream_content_hash(dataset, harvest_object.source),
                },
                {
                    "key": "source_datajson_identifier",
                    "value": True,
                },
                {
                    "key": "harvest_source_id",
                    "value": harvest_object.harvest_source_id,
                },
                {
                    "key": "harvest_object_id",
                    "value": harvest_object.id,
                },
            ]
        }

        extras = pkg["extras"]
        unmapped = []

        for key, value in dataset_processed.iteritems():
            if key in skip_processed:
                continue
            new_key = mapping_processed.get(key)
            if not new_key:
                unmapped.append(key)
                continue
            if not value:
                continue

            if new_key.startswith('extras__'):
                extras.append({"key": new_key[8:], "value": value})
            else:
                pkg[new_key] = value

        # pick a fix number of unmapped entries and put into extra
        if unmapped:
            unmapped.sort()
            del unmapped[100:]
            for key in unmapped:
                value = dataset_processed.get(key, "")
                if value is not None: extras.append({"key": key, "value": value})

        # Set specific information about the dataset.
        self.set_dataset_info(pkg, dataset_processed, dataset_defaults)
    
        # Try to update an existing package with the ID set in harvest_object.guid. If that GUID
        # corresponds with an existing package, get its current metadata.
        try:
            existing_pkg = get_action('package_show')(self.context(), { "id": harvest_object.guid })
        except NotFound:
            existing_pkg = None
      
        if existing_pkg:
            # Update the existing metadata with the new information.
            
            # But before doing that, try to avoid replacing existing resources with new resources
            # my assigning resource IDs where they match up.
            for res in pkg.get("resources", []):
                for existing_res in existing_pkg.get("resources", []):
                    if res["url"] == existing_res["url"]:
                        res["id"] = existing_res["id"]
            
            existing_pkg.update(pkg) # preserve other fields that we're not setting, but clobber extras
            pkg = existing_pkg
            
            log.warn('updating package %s (%s) from %s' % (pkg["name"], pkg["id"], harvest_object.source.url))
            pkg = get_action('package_update')(self.context(), pkg)
        else:
            # It doesn't exist yet. Create a new one.
            try:
                pkg = get_action('package_create')(self.context(), pkg)
                log.warn('created package %s (%s) from %s' % (pkg["name"], pkg["id"], harvest_object.source.url))
            except:
                log.error('failed to create package %s from %s' % (pkg["name"], harvest_object.source.url))
                raise

        # Flag the other HarvestObjects linking to this package as not current anymore
        for ob in model.Session.query(HarvestObject).filter_by(package_id=pkg["id"]):
            ob.current = False
            ob.save()

        # Flag this HarvestObject as the current harvest object
        harvest_object.package_id = pkg['id']
        harvest_object.current = True
        harvest_object.save()

        # Now that the package and the harvest source are associated, re-index the
        # package so it knows it is part of the harvest source. The CKAN harvester
        # does this by creating the association before the package is saved by
        # overriding the GUID creation on a new package. That's too difficult.
        # So here we end up indexing twice.
        PackageSearchIndex().index_package(pkg) 

        return True
        
    def make_upstream_content_hash(self, datasetdict, harvest_source):
        return hashlib.sha1(json.dumps(datasetdict, sort_keys=True)
            + "|" + harvest_source.config + "|" + self.HARVESTER_VERSION).hexdigest()
        
    def find_extra(self, pkg, key):
        for extra in pkg["extras"]:
            if extra["key"] == key:
                return extra["value"]
        return None

    def make_package_name(self, title, exclude_existing_package, for_deletion):
        '''
        Creates a URL friendly name from a title

        If the name already exists, it will add some random characters at the end
        '''

        name = munge_title_to_name(title).replace('_', '-')
        if for_deletion: name = "deleted-" + name
        while '--' in name:
            name = name.replace('--', '-')
        name = name[0:90] # max length is 100

        # Is this slug already in use (and if we're updating a package, is it in
        # use by a different package?).
        pkg_obj = Session.query(Package).filter(Package.name == name).filter(Package.id != exclude_existing_package).first()
        if not pkg_obj:
            # The name is available, so use it. Note that if we're updating an
            # existing package we will be updating this package's URL, so incoming
            # links may break.
            return name

        if exclude_existing_package:
            # The name is not available, and we're updating a package. Chances
            # are the package's name already had some random string attached
            # to it last time. Prevent spurrious updates to the package's URL
            # (choosing new random text) by just reusing the existing package's
            # name.
            pkg_obj = Session.query(Package).filter(Package.id == exclude_existing_package).first()
            if pkg_obj: # the package may not exist yet because we may be passed the desired package GUID before a new package is instantiated
                return pkg_obj.name

        # Append some random text to the URL. Hope that with five character
        # there will be no collsion.
        return name + "-" + str(uuid.uuid4())[:5]
