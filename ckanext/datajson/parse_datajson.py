from ckan.lib.munge import munge_title_to_name

import re

def parse_datajson_entry(datajson, package, defaults):
	package["tags"] = [ { "name": munge_title_to_name(t) } for t in
		package.get("tags", "") if t.strip() != ""]

	# if distribution is empty, assemble it with root level accessURL and format.
	# but firstly it can be an ill-formated dict.
	distribution = datajson.get("distribution", [])
	if isinstance(distribution, dict): distribution = [distribution]
	if not isinstance(distribution, list): distribution = []

	if not distribution:
		for url in ("accessurl", "webservice"):
			if datajson.get(url, "").strip():
				d = {
					url: datajson.get(url, ""),
					"format": datajson.get("format", ""),
					"mimetype": datajson.get("format", ""),
				}
				distribution.append(d)

	datajson["distribution"] = distribution

	for d in datajson.get("distribution", []):
		if d.get("accessurl", "").strip() != "" or d.get("webservice", "").strip() != "":
			r = {
				"url": d["accessurl"] if d.get("accessurl", "").strip() != "" else d["webservice"],
				"format": d.get("format", ""),
				"mimetype": d.get("format", ""),
			}
			package["resources"].append(r)
	
def extra(package, key, value):
	if not value: return
	package.setdefault("extras", []).append({ "key": key, "value": value })
	
def normalize_format(format, raise_on_unknown=False):
	if format is None: return
	# Format should be a file extension. But sometimes Socrata outputs a MIME type.
	format = format.lower()
	m = re.match(r"((application|text)/(\S+))(; charset=.*)?", format)
	if m:
		if m.group(1) == "text/plain": return "Text"
		if m.group(1) == "application/zip": return "ZIP"
		if m.group(1) == "application/vnd.ms-excel": return "XLS"
		if m.group(1) == "application/x-msaccess": return "Access"
		if raise_on_unknown: raise ValueError() # caught & ignored by caller
		return "Other"
	if format == "text": return "Text"
	if raise_on_unknown and "?" in format: raise ValueError() # weird value we should try to filter out; exception is caught & ignored by caller
	return format.upper() # hope it's one of our formats by converting to upprecase
