try:
    from collections import OrderedDict  # 2.7
except ImportError:
    from sqlalchemy.util import OrderedDict

import logging
import string

import ckan.model as model


log = logging.getLogger('datajson')


def get_facet_fields():
    # Return fields that we'd like to add to default CKAN faceting. This really has
    # nothing to do with exporting data.json but it's probably a common consideration.
    facets = OrderedDict()
    facets[
        # using "author" produces weird results because the Solr schema indexes it as "text" rather than "string"
        "Agency"] = "Publishers"
    # search facets remove spaces from field names
    facets["SubjectArea1"] = "Subjects"
    return facets


def make_datajson_entry(package):
    return OrderedDict([
        ("title", package["title"]),
        ("description", package["notes"]),
        ("keyword", [t["display_name"] for t in package["tags"]]),
        ("modified", extra(package, "Date Updated")),
        ("publisher", package["author"]),
        ("bureauCode", extra(package, "Bureau Code").split(" ") if extra(package, "Bureau Code") else None),
        ("programCode", extra(package, "Program Code").split(" ") if extra(package, "Program Code") else None),
        ("contactPoint", extra(package, "Contact Name")),
        ("mbox", extra(package, "Contact Email")),
        ("identifier", package["id"]),
        ("accessLevel", extra(package, "Access Level", default="public")),
        ("accessLevelComment", extra(package, "Access Level Comment")),
        ("dataDictionary", extra(package, "Data Dictionary")),
        ("accessURL", get_primary_resource(package).get("url", None)),
        ("webService", get_api_resource(package).get("url", None)),
        ("format", extension_to_mime_type(get_primary_resource(package).get("format", None)) ),
        ("license", extra(package, "License Agreement")),
        ("spatial", extra(package, "Geographic Scope")),
        ("temporal", build_temporal(package)),
        ("issued", extra(package, "Date Released")),
        ("accrualPeriodicity", extra(package, "Publish Frequency")),
        ("language", extra(package, "Language")),
        ("PrimaryITInvestmentUII", extra(package, "PrimaryITInvestmentUII")),
        ("granularity", "/".join(
            x for x in [extra(package, "Unit of Analysis"), extra(package, "Geographic Granularity")] if x != None)),
        ("dataQuality", extra(package, "Data Quality Met", default="true") == "true"),
        ("theme", [s for s in (
            extra(package, "Subject Area 1"), extra(package, "Subject Area 2"), extra(package, "Subject Area 3")) if
                   s != None]),
        ("references", [s for s in [extra(package, "Technical Documentation")] if s != None]),
        ("landingPage", package["url"]),
        ("systemOfRecords", extra(package, "System Of Records")),
        ("distribution",
         [
             OrderedDict([
                 ("identifier", r["id"]),  # NOT in POD standard, but useful for conversion to JSON-LD
                 ("accessURL", r["url"]),
                 ("format", r.get("mimetype", extension_to_mime_type(r["format"]))),
             ])
             for r in package["resources"]
             if r["format"].lower() not in ("api", "query tool", "widget")
         ]),
    ])


def extra(package, key, default=None):
    # Retrieves the value of an extras field.
    for extra in package["extras"]:
        if extra["key"] == key:
            return extra["value"]
    return default


def get_best_resource(package, acceptable_formats, unacceptable_formats=None):
    resources = list(r for r in package["resources"] if r["format"].lower() in acceptable_formats)
    if len(resources) == 0:
        if unacceptable_formats:
            # try at least any resource that's not unacceptable
            resources = list(r for r in package["resources"] if r["format"].lower() not in unacceptable_formats)
        if len(resources) == 0:
            # there is no acceptable resource to show
            return {}
    else:
        resources.sort(key=lambda r: acceptable_formats.index(r["format"].lower()))
    return resources[0]


def get_primary_resource(package):
    # Return info about a "primary" resource. Select a good one.
    return get_best_resource(package, ("csv", "xls", "xml", "text", "zip", "rdf"), ("api", "query tool", "widget"))


def get_api_resource(package):
    # Return info about an API resource.
    return get_best_resource(package, ("api", "query tool"))


def build_temporal(package):
    # Build one dataset entry of the data.json file.
    temporal = ""
    if extra(package, "Coverage Period Fiscal Year Start"):
        temporal = "FY" + extra(package, "Coverage Period Fiscal Year Start").replace(" ", "T").replace("T00:00:00", "")
    else:
        temporal = extra(package, "Coverage Period Start", "Unknown").replace(" ", "T").replace("T00:00:00", "")
    temporal += "/"
    if extra(package, "Coverage Period Fiscal Year End"):
        temporal += "FY" + extra(package, "Coverage Period Fiscal Year End").replace(" ", "T").replace("T00:00:00", "")
    else:
        temporal += extra(package, "Coverage Period End", "Unknown").replace(" ", "T").replace("T00:00:00", "")
    if temporal == "Unknown/Unknown": return None
    return temporal


def extension_to_mime_type(file_ext):
    if file_ext is None: return None
    ext = {
        "csv": "text/csv",
        "xls": "application/vnd.ms-excel",
        "xml": "application/xml",
        "rdf": "application/rdf+xml",
        "json": "application/json",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text": "text/plain",
        "feed": "application/rss+xml",
    }
    return ext.get(file_ext.lower(), "application/unknown")


class JsonExportBuilder:
    @staticmethod
    def make_datajson_export_catalog(datasets):
        catalog = OrderedDict([
            ('conformsTo', 'https://project-open-data.cio.gov/v1.1/schema'),  # requred
            ('describedBy', 'https://project-open-data.cio.gov/v1.1/schema/catalog.json'),  # optional
            ('@context', 'https://project-open-data.cio.gov/v1.1/schema/catalog.jsonld'),  # optional
            ('@type', 'dcat:Catalog'),  # optional
            ('dataset', datasets),  # required
        ])
        return catalog

    @staticmethod
    def make_datajson_export_entry(package):
        # extras is a list of dicts [{},{}, {}]. For each dict, extract the key, value entries into a new dict
        extras = dict([(x['key'], x['value']) for x in package['extras']])

        parent_dataset_id = extras.get('parent_dataset')
        if parent_dataset_id:
            parent = model.Package.get(parent_dataset_id)
            parent_uid = parent.extras.col.target['unique_id'].value
            if parent_uid:
                parent_dataset_id = parent_uid

        # if resource format is CSV then convert it to text/csv
        # Resource format has to be in 'csv' format for automatic datastore push.
        for r in package["resources"]:
            if r["format"].lower() == "csv":
                r["format"] = "text/csv"
            if r["format"].lower() == "json":
                r["format"] = "application/json"
            if r["format"].lower() == "pdf":
                r["format"] = "application/pdf"

        try:
            retlist = [
                ("@type", "dcat:Dataset"),  # optional

                ("title", JsonExportBuilder.strip_if_string(package["title"])),  # required

                # ("accessLevel", 'public'),  # required
                ("accessLevel", JsonExportBuilder.strip_if_string(extras.get('public_access_level'))),  # required

                # ("accrualPeriodicity", "R/P1Y"),  # optional
                # ('accrualPeriodicity', 'accrual_periodicity'),
                ('accrualPeriodicity', JsonExportBuilder.get_accrual_periodicity(extras.get('accrual_periodicity'))),
                # optional

                ("conformsTo", JsonExportBuilder.strip_if_string(extras.get('conforms_to'))),  # optional

                # ('contactPoint', OrderedDict([
                # ("@type", "vcard:Contact"),
                # ("fn", "Jane Doe"),
                # ("hasEmail", "mailto:jane.doe@agency.gov")
                # ])),  # required
                ('contactPoint', JsonExportBuilder.get_contact_point(extras, package)),  # required

                ("dataQuality", JsonExportBuilder.strip_if_string(extras.get('data_quality'))),
                # required-if-applicable

                ("describedBy", JsonExportBuilder.strip_if_string(extras.get('data_dictionary'))),  # optional
                ("describedByType", JsonExportBuilder.strip_if_string(extras.get('data_dictionary_type'))),  # optional

                ("description", JsonExportBuilder.strip_if_string(package["notes"])),  # required

                # ("description", 'asdfasdf'),  # required

                ("identifier", JsonExportBuilder.strip_if_string(extras.get('unique_id'))),  # required
                # ("identifier", 'asdfasdfasdf'),  # required

                ("isPartOf", parent_dataset_id),  # optional
                ("issued", JsonExportBuilder.strip_if_string(extras.get('release_date'))),  # optional

                # ("keyword", ['a', 'b']),  # required
                ("keyword", [t["display_name"] for t in package["tags"]]),  # required

                ("landingPage", JsonExportBuilder.strip_if_string(extras.get('homepage_url'))),  # optional

                ("license", JsonExportBuilder.strip_if_string(extras.get("license_new"))),  # required-if-applicable

                ("modified", JsonExportBuilder.strip_if_string(extras.get("modified"))),  # required

                ("primaryITInvestmentUII", JsonExportBuilder.strip_if_string(extras.get('primary_it_investment_uii'))),
                # optional

                # ('publisher', OrderedDict([
                # ("@type", "org:Organization"),
                # ("name", "Widget Services")
                # ])),  # required
                # ("publisher", get_publisher_tree(extras)),  # required
                ("publisher", JsonExportBuilder.get_publisher_tree_wrong_order(extras)),  # required

                ("rights", JsonExportBuilder.strip_if_string(extras.get('access_level_comment'))),  # required

                ("spatial", JsonExportBuilder.strip_if_string(package.get("spatial"))),  # required-if-applicable

                ('systemOfRecords', JsonExportBuilder.strip_if_string(extras.get('system_of_records'))),  # optional

                ("temporal", JsonExportBuilder.strip_if_string(extras.get('temporal'))),  # required-if-applicable

                ("distribution", JsonExportBuilder.generate_distribution(package)),  # required-if-applicable

                # ("distribution",
                # #TODO distribution should hide any key/value pairs where value is "" or None (e.g. format)
                # [
                # OrderedDict([
                # ("downloadURL", r["url"]),
                # ("mediaType", r["formatReadable"]),
                # ])
                # for r in package["resources"]
                # ])
            ]

            for pair in [
                ('bureauCode', 'bureau_code'),  # required
                ('language', 'language'),  # optional
                ('programCode', 'program_code'),  # required
                ('references', 'related_documents'),  # optional
                ('theme', 'category'),  # optional
            ]:
                JsonExportBuilder.split_multiple_entries(retlist, extras, pair)

        except KeyError as e:
            log.warn("Invalid field detected for package with id=[%s], title=['%s']: '%s'", package.get('id'),
                     package.get('title'), e)
            return

        # # TODO this is a lazy hack to make sure we don't have redundant fields when the free form key/value pairs are added
        # extras_to_filter_out = ['publisher', 'contact_name', 'contact_email', 'unique_id', 'public_access_level',
        # 'data_dictionary', 'bureau_code', 'program_code', 'access_level_comment', 'license_title',
        # 'spatial', 'temporal', 'release_date', 'accrual_periodicity', 'language', 'granularity',
        # 'data_quality', 'size', 'homepage_url', 'rss_feed', 'category', 'related_documents',
        # 'system_of_records', 'system_of_records_none_related_to_this_dataset', 'tags',
        # 'extrasRollup', 'format', 'accessURL', 'notes', 'publisher_1', 'publisher_2', 'publisher_3',
        # 'publisher_4', 'publisher_5']
        #
        # # Append any free extras (key/value pairs) that aren't part of common core but have been associated with the dataset
        # # TODO really hackey, short on time, had to hardcode a lot of the names to remove. there's much better ways, maybe
        # # generate a list of keys to ignore by calling a specific function to get the extras
        # retlist_keys = [x for x, y in retlist]
        # extras_keys = set(extras.keys()) - set(extras_to_filter_out)
        #
        # for key in extras_keys:
        # convertedKey = underscore_to_camelcase(key)
        # if convertedKey not in retlist_keys:
        # retlist.append((convertedKey, extras[key]))

        # Remove entries where value is None, "", or empty list []
        striped_retlist = [(x, y) for x, y in retlist if y is not None and y != "" and y != []]
        striped_retlist_keys = [x for x, y in striped_retlist]


        # If a required metadata field was removed, return empty string
        # for required_field in ["accessLevel", "bureauCode", "contactPoint", "description", "identifier", "keyword",
        # "modified", "programCode", "publisher", "title"]:
        # if required_field not in striped_retlist_keys:
        # log.warn("Missing required field detected for package with id=[%s], title=['%s']: '%s'",
        # package.get('id'), package.get('title'), required_field)
        # return

        # When saved from UI DataQuality value is stored as "on" instead of True.
        # Check if value is "on" and replace it with True.
        striped_retlist_dict = OrderedDict(striped_retlist)
        if striped_retlist_dict.get('dataQuality') == "on" \
                or striped_retlist_dict.get('dataQuality') == "true" \
                or striped_retlist_dict.get('dataQuality') == "True":
            striped_retlist_dict['dataQuality'] = True
        elif striped_retlist_dict.get('dataQuality') == "false" \
                or striped_retlist_dict.get('dataQuality') == "False":
            striped_retlist_dict['dataQuality'] = False

        from datajsonvalidator import do_validation

        errors = []
        try:
            do_validation([dict(striped_retlist_dict)], errors)
        except Exception as e:
            errors.append(("Internal Error", ["Something bad happened: " + unicode(e)]))
        if len(errors) > 0:
            for error in errors:
                log.warn(error)
            return

        return striped_retlist_dict


    # used by get_accrual_periodicity
    accrual_periodicity_dict = {
        'completely irregular': 'irregular',
        'decennial': 'R/P10Y',
        'quadrennial': 'R/P4Y',
        'annual': 'R/P1Y',
        'bimonthly': 'R/P2M',  # or R/P0.5M
        'semiweekly': 'R/P3.5D',
        'daily': 'R/P1D',
        'biweekly': 'R/P2W',  # or R/P0.5W
        'semiannual': 'R/P6M',
        'biennial': 'R/P2Y',
        'triennial': 'R/P3Y',
        'three times a week': 'R/P0.33W',
        'three times a month': 'R/P0.33M',
        'continuously updated': 'R/PT1S',
        'monthly': 'R/P1M',
        'quarterly': 'R/P3M',
        'semimonthly': 'R/P0.5M',
        'three times a year': 'R/P4M',
        'weekly': 'R/P1W'
    }

    @staticmethod
    def get_accrual_periodicity(frequency):
        return JsonExportBuilder.accrual_periodicity_dict.get(str(frequency).lower().strip(), frequency)

    @staticmethod
    def generate_distribution(package):
        arr = []
        for r in package["resources"]:
            resource = [("@type", "dcat:Distribution")]
            rkeys = r.keys()
            if 'url' in rkeys:
                res_url = JsonExportBuilder.strip_if_string(r.get('url'))
                if res_url:
                    if 'api' == r.get('resource_type') or 'accessurl' == r.get('resource_type'):
                        resource += [("accessURL", res_url)]
                    else:
                        resource += [("downloadURL", res_url)]
                        if 'format' in rkeys:
                            res_format = JsonExportBuilder.strip_if_string(r.get('format'))
                            if res_format:
                                resource += [("mediaType", res_format)]
                        else:
                            log.warn("Missing mediaType for resource in package ['%s']", package.get('id'))
            else:
                log.warn("Missing downloadURL for resource in package ['%s']", package.get('id'))

            # if 'accessURL_new' in rkeys:
            # res_access_url = JsonExportBuilder.strip_if_string(r.get('accessURL_new'))
            # if res_access_url:
            # resource += [("accessURL", res_access_url)]

            if 'formatReadable' in rkeys:
                res_attr = JsonExportBuilder.strip_if_string(r.get('formatReadable'))
                if res_attr:
                    resource += [("format", res_attr)]

            if 'name' in rkeys:
                res_attr = JsonExportBuilder.strip_if_string(r.get('name'))
                if res_attr:
                    resource += [("title", res_attr)]

            if 'notes' in rkeys:
                res_attr = JsonExportBuilder.strip_if_string(r.get('notes'))
                if res_attr:
                    resource += [("description", res_attr)]

            if 'conformsTo' in rkeys:
                res_attr = JsonExportBuilder.strip_if_string(r.get('conformsTo'))
                if res_attr:
                    resource += [("conformsTo", res_attr)]

            if 'describedBy' in rkeys:
                res_attr = JsonExportBuilder.strip_if_string(r.get('describedBy'))
                if res_attr:
                    resource += [("describedBy", res_attr)]

            if 'describedByType' in rkeys:
                res_attr = JsonExportBuilder.strip_if_string(r.get('describedByType'))
                if res_attr:
                    resource += [("describedByType", res_attr)]

            striped_resource = [(x, y) for x, y in resource if y is not None and y != "" and y != []]

            arr += [OrderedDict(striped_resource)]

        return arr

    @staticmethod
    def get_contact_point(extras, package):
        for required_field in ["contact_name", "contact_email"]:
            if required_field not in extras.keys():
                raise KeyError(required_field)

        email = JsonExportBuilder.strip_if_string(extras['contact_email'])
        if email is None or '@' not in email:
            raise KeyError(required_field)

        fn = JsonExportBuilder.strip_if_string(extras['contact_name'])
        if fn is None:
            raise KeyError(required_field)

        contact_point = OrderedDict([
            ('@type', 'vcard:Contact'),  # optional
            ('fn', fn),  # required
            ('hasEmail', 'mailto:' + email),  # required
        ])
        return contact_point

    @staticmethod
    def extra(package, key, default=None):
        # Retrieves the value of an extras field.
        for extra in package["extras"]:
            if extra["key"] == key:
                return extra["value"]
        return default

    @staticmethod
    def get_publisher_tree_wrong_order(extras):
        publisher = JsonExportBuilder.strip_if_string(extras.get('publisher'))
        if publisher is None:
            raise KeyError('publisher')

        organization_list = list()
        organization_list.append([
            ('@type', 'org:Organization'),  # optional
            ('name', publisher),  # required
        ])

        for i in range(1, 6):
            key = 'publisher_' + str(i)
            if key in extras and extras[key] and JsonExportBuilder.strip_if_string(extras[key]):
                organization_list.append([
                    ('@type', 'org:Organization'),  # optional
                    ('name', JsonExportBuilder.strip_if_string(extras[key])),  # required
                ])

        size = len(organization_list)

        # [OSCIT, GSA]
        # organization_list.reverse()
        # [GSA, OSCIT]

        tree = False
        for i in range(0, size):
            if tree:
                organization_list[i] += [('subOrganizationOf', OrderedDict(tree))]
            tree = organization_list[i]

        return OrderedDict(tree)

    @staticmethod
    def underscore_to_camelcase(value):
        """
        Convert underscored strings to camel case, e.g. one_two_three to oneTwoThree
        """

        def camelcase():
            yield unicode.lower
            while True:
                yield unicode.capitalize

        c = camelcase()
        return "".join(c.next()(x) if x else '_' for x in value.split("_"))

    @staticmethod
    def get_best_resource(package, acceptable_formats):
        resources = list(r for r in package["resources"] if r["format"].lower() in acceptable_formats)
        if len(resources) == 0: return {}
        resources.sort(key=lambda r: acceptable_formats.index(r["format"].lower()))
        return resources[0]

    @staticmethod
    def strip_if_string(val):
        if isinstance(val, (str, unicode)):
            val = val.strip()
            if '' == val:
                val = None
        return val


    @staticmethod
    def get_primary_resource(package):
        # Return info about a "primary" resource. Select a good one.
        return JsonExportBuilder.get_best_resource(package, ("csv", "xls", "xml", "text", "zip", "rdf"))


    @staticmethod
    def get_api_resource(package):
        # Return info about an API resource.
        return JsonExportBuilder.get_best_resource(package, ("api", "query tool"))


    @staticmethod
    def split_multiple_entries(retlist, extras, names):
        found_element = string.strip(extras.get(names[1], ""))
        if found_element:
            retlist.append(
                (names[0], [string.strip(x) for x in string.split(found_element, ',')])
            )
