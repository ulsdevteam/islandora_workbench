import os
import sys
import json
import csv
import logging
import datetime
import collections
import requests_cache
from workbench_utils import *
from progress_bar import InitBar


class WorkbenchExportBase:
    def __init__(self, config, args=None):
        self.config = config
        self.args = args
        self.field_definitions = get_field_definitions(config, "node")
        self.required_fields = [
            "created",
            "changed",
            "uid",
            "uuid",
            "langcode",
            "title",
            "node_id",
        ]
        self.seen_nids = set()
        self.pbar = InitBar() if config.get("progress_bar") else None

    @staticmethod
    def deduplicate_list(input_list):
        """Remove duplicates while preserving order."""
        seen = set()
        return [x for x in input_list if not (x in seen or seen.add(x))]

    def initialize_csv_writer(self, csv_file, field_names, export_mode=False):
        """Initialize CSV writer with optional metadata rows for export mode."""
        writer = csv.DictWriter(csv_file, fieldnames=field_names, lineterminator="\n")
        writer.writeheader()

        if export_mode and self.field_definitions:
            self._write_metadata_rows(writer, field_names)

        return writer

    def _write_metadata_rows(self, writer, field_names):
        """Write field labels and cardinality rows for export mode."""
        field_labels = collections.OrderedDict()
        for field_name in field_names:
            if (
                field_name in self.field_definitions
                and self.field_definitions[field_name]["label"] != ""
            ):
                field_labels[field_name] = self.field_definitions[field_name]["label"]
            elif field_name == "REMOVE THIS COLUMN (KEEP THIS ROW)":
                field_labels[field_name] = "LABEL (REMOVE THIS ROW)"
            elif field_name == "node_id":
                field_labels[field_name] = "Node ID"
            elif field_name == "created":
                field_labels[field_name] = "Created"
            elif field_name == "changed":
                field_labels[field_name] = "Changed"
            elif field_name == "uid":
                field_labels[field_name] = "User ID"
            elif field_name == "uuid":
                field_labels[field_name] = "UUID"
            elif field_name == "langcode":
                field_labels[field_name] = "Drupal language code"
            elif field_name == "url_alias":
                field_labels[field_name] = "URL alias"
            else:
                field_labels[field_name] = ""
        writer.writerow(field_labels)

        cardinality = collections.OrderedDict()
        cardinality["REMOVE THIS COLUMN (KEEP THIS ROW)"] = (
            "NUMBER OF VALUES ALLOWED (REMOVE THIS ROW)"
        )
        cardinality["node_id"] = "1"
        cardinality["uuid"] = "1"
        cardinality["uid"] = "1"
        cardinality["langcode"] = "1"
        cardinality["created"] = "1"
        cardinality["changed"] = "1"
        cardinality["title"] = "1"
        cardinality["url_alias"] = "1"

        for field_name in self.field_definitions:
            if field_name in field_names:
                cardinality[field_name] = (
                    "unlimited"
                    if self.field_definitions[field_name]["cardinality"] == -1
                    else str(self.field_definitions[field_name]["cardinality"])
                )
        writer.writerow(cardinality)

    def process_file_data(self, node_id, media_use_term_id=None, media_list=None):
        """Handle file processing (download or URL) based on config."""
        if self.config.get("export_file_url_instead_of_download", False):
            result = get_media_file_url(
                self.config,
                node_id,
                media_use_term_id=media_use_term_id,
                media_list=media_list,
            )
        else:
            result = download_file_from_drupal(
                self.config,
                node_id,
                media_use_term_id=media_use_term_id,
                media_list=media_list,
            )
        return result if result else ""  # Avoid 'False' values in file columns.

    def log_progress(
        self, message, row_count=None, total_rows=None, level=logging.INFO
    ):
        """Standardized progress logging with optional progress bar and log levels.

        Args:
            message: The message to log
            row_count: Current row count (for progress bar)
            total_rows: Total rows (for progress bar)
            level: Logging level (e.g., logging.INFO, logging.WARNING, logging.ERROR)
        """
        if (
            self.config.get("progress_bar")
            and self.pbar
            and row_count is not None
            and total_rows is not None
        ):
            self.pbar(get_percentage(row_count, total_rows))
        else:
            logging_level = logging.getLevelName(level)
            if logging_level == "INFO":
                print(message)
            else:
                print(f"{logging.getLevelName(level)}: {message}")

        # Log to file with appropriate level
        if level == logging.INFO:
            logging.info(message)
        elif level == logging.WARNING:
            logging.warning(message)
        elif level == logging.ERROR:
            logging.error(message)
        elif level == logging.DEBUG:
            logging.debug(message)
        else:
            logging.info(message)  # default to info if unknown level

    def row_log_suffix(self, row):
        and_files = ""
        if self.needs_file_column:
            if self.config.get("export_file_url_instead_of_download", False):
                and_files = " and file URL(s)"
            else:
                and_files = " and file(s)"

        return and_files

    def extract_node_id(self, node):
        """Extract and validate node ID."""
        try:
            return node["nid"][0]["value"]
        except (KeyError, IndexError):
            message = "Skipping node with missing/invalid NID"
            self.log_progress(message, level=logging.WARNING)
            return None

    def validate_content_type(self, node, nid):
        """Verify node matches configured content type."""
        try:
            node_type = node["type"][0]["target_id"]
        except (KeyError, IndexError):
            node_type = "unknown"

        if node_type != self.config["content_type"]:
            message = (
                f"Node {nid} not written to output CSV because its content type {node_type}"
                + f' does not match the "content_type" configuration setting.'
            )
            self.log_progress(message, level=logging.ERROR)
            return False
        return True

    def parse_json_response(self, response):
        """Safely parse JSON from response."""
        try:
            return json.loads(response.text)
        except json.decoder.JSONDecodeError as e:
            message = f"Failed to decode JSON: {str(e)}"
            self.log_progress(message, level=logging.WARNING)
            return []

    def needs_file_column(self):
        """Check if we need file column in CSV."""
        return self.config.get(
            "export_file_url_instead_of_download", False
        ) or self.config.get("export_file_directory")

    def get_additional_files_config(self):
        """Get additional files configuration as OrderedDict."""
        if "additional_files" not in self.config:
            return collections.OrderedDict()

        additional_files = collections.OrderedDict()
        for entry in self.config["additional_files"]:
            for col_name, media_use_uri in entry.items():
                additional_files[col_name] = media_use_uri
        return additional_files

    def execute_post_export_script(self, response, node_json):
        """Execute node-specific post-export scripts, if any are configured."""
        if len(self.config.get("node_post_export", [])) > 0:
            for command in self.config.get("node_post_export", []):
                (
                    post_task_output,
                    post_task_return_code,
                ) = execute_entity_post_task_script(
                    command,
                    self.args.config,
                    response.status_code,
                    node_json,
                )
                if post_task_return_code == 0:
                    logging.info(
                        "Post node export script " + command + " executed successfully."
                    )
                else:
                    logging.error("Post node export script " + command + " failed.")


class CSVExporter(WorkbenchExportBase):
    def __init__(self, config, args=None):
        super().__init__(config, args)
        self.csv_data = get_csv_data(config)
        self.required_fields = [
            "created",
            "changed",
            "uid",
            "uuid",
            "langcode",
            "title",
            "url_alias",
            "node_id",
            "REMOVE THIS COLUMN (KEEP THIS ROW)",
        ]

    def setup_csv_output_path(self):
        """Set up CSV output path for CSV export."""
        if self.config["export_csv_file_path"]:
            csv_path = self.config["export_csv_file_path"]
        else:
            csv_path = os.path.join(
                self.config["input_dir"],
                self.config["input_csv"] + ".csv_file_with_field_values",
            )

        if os.path.exists(csv_path):
            os.remove(csv_path)
        return csv_path

    def prepare_headers(self):
        """Generate deduplicated list of CSV column headers for export_csv."""
        field_names = list(self.field_definitions.keys())
        field_names.append("url_alias")

        if len(self.config["export_csv_field_list"]) > 0:
            field_names = self.config["export_csv_field_list"]

        # Add required fields at beginning
        for field_name in self.required_fields:
            field_names.insert(0, field_name)

        deduped_field_names = self.deduplicate_list(field_names)

        # We always include 'node_id and 'REMOVE THIS COLUMN (KEEP THIS ROW)'.
        if "node_id" not in deduped_field_names:
            deduped_field_names.insert(0, "url_alias")
            deduped_field_names.insert(0, "node_id")
            deduped_field_names.insert(0, "REMOVE THIS COLUMN (KEEP THIS ROW)")

        # Handle file columns
        if self.needs_file_column() and "file" not in deduped_field_names:
            deduped_field_names.append("file")

        # Add additional files columns
        additional_files_entries = self.get_additional_files_config()
        if additional_files_entries:
            for column in additional_files_entries.keys():
                if column not in deduped_field_names:
                    deduped_field_names.append(column)

        return deduped_field_names

    def validate_and_get_node_id(self, csv_row):
        """Validate and get node ID from CSV row."""
        node_id = csv_row["node_id"]
        if not value_is_numeric(node_id):
            node_id = get_nid_from_url_alias(self.config, node_id)

        if not ping_node(self.config, node_id):
            self.log_progress(
                f"Node {node_id} not found/accessible, skipping export.",
                level=logging.WARNING,
            )
            return None

        return node_id

    def fetch_node_json(self, node_id):
        """Fetch node JSON from Drupal."""
        url = f"{self.config['host']}/node/{node_id}?_format=json"
        response = issue_request(self.config, "GET", url)

        if response.status_code != 200:
            self.log_progress(
                f"Error retrieving node {node_id}: HTTP {response.status_code}",
                level=logging.WARNING,
            )
            return None

        return json.loads(response.text)

    def process_node_fields_to_row(self, node_json, field_names):
        """Process node fields into a dictionary for CSV output."""
        row = collections.OrderedDict()
        nid = self.extract_node_id(node_json)

        if not nid:
            return None

        row["node_id"] = nid
        row["title"] = node_json.get("title", [{}])[0].get("value", "No title")
        row["url_alias"] = node_json.get("path", [{}])[0].get("alias", "")
        row["uuid"] = node_json.get("uuid", [{}])[0].get("value", "")
        row["uid"] = node_json.get("uid", [{}])[0].get("target_id", "")
        row["created"] = node_json.get("created", [{}])[0].get("value", "")
        row["changed"] = node_json.get("changed", [{}])[0].get("value", "")
        row["langcode"] = node_json.get("langcode", [{}])[0].get("value", "")

        # Process fields
        for field in field_names:
            if field.startswith("field_") and field in node_json:
                try:
                    row[field] = serialize_field_json(
                        self.config, self.field_definitions, field, node_json[field]
                    )
                except Exception as e:
                    self.log_progress(
                        f"Error serializing {field} for node {nid}: {str(e)}",
                        level=logging.ERROR,
                    )
                    row[field] = "SERIALIZATION_ERROR"

        return row

    def process_node_row(self, node_json, field_names):
        """Common processing for node rows."""
        row = self.process_node_fields_to_row(node_json, field_names)
        if not row:
            return None

        media_list = get_media_list(self.config, row["node_id"])
        additional_files_entries = self.get_additional_files_config()

        if self.needs_file_column():
            row["file"] = self.process_file_data(row["node_id"], media_list=media_list)

        if additional_files_entries:
            for col_name, media_use_uri in additional_files_entries.items():
                row[col_name] = self.process_file_data(
                    row["node_id"],
                    media_use_term_id=media_use_uri,
                    media_list=media_list,
                )

        return row

    def export(self):
        """Main export method for CSV export."""
        csv_file_path = self.setup_csv_output_path()
        field_names = self.prepare_headers()

        with open(csv_file_path, "a+", encoding="utf-8") as csv_file:
            writer = self.initialize_csv_writer(csv_file, field_names, True)
            self._process_nodes(writer, field_names)

        return csv_file_path

    def _process_nodes(self, writer, field_names):
        """Process all nodes for CSV export."""
        csv_data_list = list(self.csv_data)
        row_count = 0

        for row in csv_data_list:
            # Delete expired items from request_cache before processing a row.
            if self.config["enable_http_cache"]:
                requests_cache.delete(expired=True)

            node_id = self.validate_and_get_node_id(row)
            if not node_id:
                continue

            node_json = self.fetch_node_json(node_id)
            if not node_json or not self.validate_content_type(node_json, node_id):
                continue

            output_row = self.process_node_row(node_json, field_names)
            if not output_row:
                continue

            writer.writerow(output_row)
            suffix = self.row_log_suffix(row)

            self.log_progress(
                f'Exporting data{suffix} for node {node_id} "{output_row["title"]}."',
                row_count,
                len(csv_data_list),
            )
            row_count += 1


class ViewExporter(WorkbenchExportBase):
    def __init__(self, config, args):
        super().__init__(config, args)
        self.view_config = self.initialize_view_config()

    def setup_csv_output_path(self):
        """Set up CSV output path with timestamp for view exports."""
        if self.config["export_csv_file_path"]:
            return self.config["export_csv_file_path"]

        config_base = os.path.basename(self.args.config).split(".")[0]
        csv_path = os.path.join(
            self.config["input_dir"],
            f"{config_base}_view_export_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.csv",
        )

        if os.path.exists(csv_path):
            os.remove(csv_path)
        return csv_path

    def initialize_view_config(self):
        """Initialize configuration for View export."""
        view_parameters = (
            "&".join(self.config["view_parameters"])
            if "view_parameters" in self.config
            else ""
        )
        return {
            "base_url": f"{self.config['host']}/{self.config['view_path'].lstrip('/')}",
            "parameters": view_parameters,
            "initial_url": f"{self.config['host']}/{self.config['view_path'].lstrip('/')}?page=0&{view_parameters}",
        }

    def verify_view_accessibility(self):
        """Verify that the View endpoint is accessible."""
        status_code = ping_view_endpoint(self.config, self.view_config["initial_url"])
        if status_code != 200:
            message = f"Cannot access View at {self.view_config['initial_url']} (HTTP {status_code})."
            self.log_progress(message, level=logging.ERROR)
            sys.exit("Error: " + message + " See log for more information.")

    def prepare_headers(self):
        """Generate deduplicated list of CSV column headers."""
        if len(self.config.get("export_csv_field_list")) == 0:
            fields = [
                "node_id",
                "title",
                "changed",
                "created",
                "uuid",
                "uid",
                "url_alias",
            ]
        else:
            fields = [
                "node_id",
                "title",
            ]

        if self.config.get("export_csv_field_list"):
            fields += [
                f for f in self.config["export_csv_field_list"] if f not in fields
            ]
        else:
            fields += [
                f for f in self.field_definitions.keys() if f.startswith("field_")
            ]

        if self.needs_file_column():
            fields.append("file")

        if "additional_files" in self.config:
            fields += self.get_additional_files_config().keys()

        return self.deduplicate_list(fields)

    def process_node_fields_to_row(self, node_json, field_names):
        """Process node fields into a dictionary for CSV output."""
        row = collections.OrderedDict()
        nid = self.extract_node_id(node_json)

        if not nid:
            return None

        row["node_id"] = nid
        row["title"] = node_json.get("title", [{}])[0].get("value", "")
        if "url_alias" in field_names:
            row["url_alias"] = node_json.get("path", [{}])[0].get("alias", "")
        if "uuid" in field_names:
            row["uuid"] = node_json.get("uuid", [{}])[0].get("value", "")
        if "uid" in field_names:
            row["uid"] = node_json.get("uid", [{}])[0].get("target_id", "")
        if "created" in field_names:
            row["created"] = node_json.get("created", [{}])[0].get("value", "")
        if "changed" in field_names:
            row["changed"] = node_json.get("changed", [{}])[0].get("value", "")
        if "langcode" in field_names:
            row["langcode"] = node_json.get("langcode", [{}])[0].get("value", "")

        # Process fields
        for field in field_names:
            if field.startswith("field_") and field in node_json:
                try:
                    row[field] = serialize_field_json(
                        self.config, self.field_definitions, field, node_json[field]
                    )
                except Exception as e:
                    self.log_progress(
                        f"Error serializing {field} for node {nid}: {str(e)}",
                        level=logging.ERROR,
                    )
                    row[field] = "SERIALIZATION_ERROR"

        return row

    def process_node_row(self, node_json, field_names):
        """Process node data into CSV row."""
        row = self.process_node_fields_to_row(node_json, field_names)
        if not row:
            return None

        media_list = get_media_list(self.config, row["node_id"])
        additional_files_entries = self.get_additional_files_config()

        if self.needs_file_column():
            row["file"] = self.process_file_data(row["node_id"], media_list=media_list)

        if additional_files_entries:
            for col_name, media_use_uri in additional_files_entries.items():
                row[col_name] = self.process_file_data(
                    row["node_id"],
                    media_use_term_id=media_use_uri,
                    media_list=media_list,
                )

        return row

    def export(self):
        """Main export method for View export."""
        self.verify_view_accessibility()
        csv_file_path = self.setup_csv_output_path()
        field_names = self.prepare_headers()

        with open(csv_file_path, "a+", encoding="utf-8") as csv_file:
            writer = self.initialize_csv_writer(csv_file, field_names)
            self._process_view_pages(writer, field_names)

        return csv_file_path

    def _process_view_pages(self, writer, field_names):
        """Process paginated View results."""
        page = 0

        while True:
            current_url = f"{self.view_config['base_url']}?page={page}&{self.view_config['parameters']}"
            response = issue_request(self.config, "GET", current_url)

            if response.status_code != 200:
                self.log_progress(
                    f"Skipping page {page} due to HTTP {response.status_code}",
                    level=logging.WARNING,
                )
                page += 1
                continue

            nodes = self.parse_json_response(response)
            if not nodes:
                break

            for node in nodes:
                if self.config.get("enable_http_cache", False):
                    requests_cache.delete(expired=True)

                nid = self.extract_node_id(node)
                if not nid or nid in self.seen_nids:
                    continue

                self.seen_nids.add(nid)
                if not self.validate_content_type(node, nid):
                    continue

                row = self.process_node_row(node, field_names)
                if row:
                    writer.writerow(row)

                    suffix = self.row_log_suffix(row)
                    self.log_progress(f"Exported node{suffix} {nid}: {row['title']}")
                    self.execute_post_export_script(response, json.dumps(node))

            page += 1
            
 
import csv
class MemberMediaExporter(WorkbenchExportBase):
    """
    Exports media files from a node and all of its recursively-discovered
    members, via the Islandora Workbench Integration module's
    "members-of-node" REST export View. Unlike CSVExporter/ViewExporter,
    this task does not assume a single content_type -- a node's members
    may be of any content type, which is exactly why this task exists as
    separate from export_csv/get_data_from_view (see the note sent
    upstream re: #503).

    Exports ALL Media Use types found on each node (not filtered to a
    single configured term), per upstream's request for a CSV manifest
    of what was exported. Each downloaded file's directory is a per-node
    subfolder of export_file_directory (export_file_directory/{node_id}/),
    to keep files from different nodes unambiguous on disk. The manifest
    CSV records, per file: node_id, media_id, media_use_type, media_index
    (1-based position among files sharing the same node_id + media_use_type,
    for cases where a node has more than one file tagged with the same
    use), and the file's full path on disk.

    Config settings this task reads (see check_input()'s
    "export_member_media" branch and WorkbenchConfig.py defaults):
        input_csv                      CSV with a node_id column, OR:
        node_id                        a single node ID (mutually exclusive)
        export_file_directory          required; root output directory
        include_member_media           default True; recurse into members
        member_media_max_depth         default None (unlimited)
        members_of_node_view_endpoint  default "/islandora_workbench_integration/members-of-node"
        export_member_media_csv_file_path
                                        default None; if unset, falls back
                                        to export_file_directory/media_manifest.csv
        export_member_media_use_types  default []/unset (all types); list of
                                        Media Use term IDs, names, or URIs to
                                        restrict export to
        export_member_media_report_only
                                        default False; if True, writes the
                                        CSV manifest without downloading
                                        anything (file_path column is blank)
    """

    def __init__(self, config, args=None):
        # Deliberately does NOT call super().__init__(), because
        # WorkbenchExportBase.__init__() unconditionally calls
        # get_field_definitions(config, "node"), which pings Drupal for
        # config["content_type"] specifically -- a member node here may be
        # of any content type, so that ping is both unnecessary and,
        # depending on what content_type happens to be set to, could
        # sys.exit() this task for a reason that has nothing to do with it.
        self.config = config
        self.args = args
        self.seen_nids = set()
        self.pbar = InitBar() if config.get("progress_bar") else None
        self.view_endpoint = config.get(
            "members_of_node_view_endpoint",
            "/islandora_workbench_integration/members-of-node",
        )

    def get_member_node_ids(self, parent_nid):
        """
        Return the direct members of a node via the members-of-node View,
        following the same page-loop shape as ViewExporter._process_view_pages(),
        but reading a flat [{"nid": "...", "field_weight_value": "...", ...}, ...]
        response instead of a JSON:API-style node collection.

        Returns a list of {"nid": str, "weight": str} dicts. An empty list
        means either the node genuinely has no members, or a page failed
        to load (logged either way; see log_progress calls below). A
        member row with no weight value gets weight=None (rare, but
        possible if field_weight isn't populated on that content type).
        """
        members = []
        page = 0

        while True:
            url = f"{self.config['host']}{self.view_endpoint}/{parent_nid}?_format=json&page={page}"
            response = issue_request(self.config, "GET", url)

            if response.status_code != 200:
                self.log_progress(
                    f"Could not retrieve page {page} of members for node {parent_nid} "
                    f"(HTTP {response.status_code}).",
                    level=logging.WARNING,
                )
                break

            rows = self.parse_json_response(response)
            if not rows:
                break

            for row in rows:
                nid = row.get("nid") if isinstance(row, dict) else None
                if nid:
                    members.append(
                        {"nid": str(nid), "weight": row.get("field_weight_value")}
                    )
                else:
                    self.log_progress(
                        f"Skipping malformed member entry under parent {parent_nid}: {row}",
                        level=logging.WARNING,
                    )

            if len(rows) < 50:
                break
            page += 1

        return members

    def collect_node_and_members(self, node_id, weight="root", depth=0):
        """
        Return {node_id: weight, ...} for this node plus, if
        include_member_media is enabled, all descendant nodes found by
        recursively walking the members-of-node View. Mirrors the
        prototype's collect_node_and_descendants(), reusing self.seen_nids
        (already provided by WorkbenchExportBase) for cycle protection.

        'weight' is this node's field_weight_value relative to its OWN
        parent (as reported by the View row that discovered it) -- used
        by the "export" filename format to keep files in reading order
        when sorted by the OS. The starting/root node of a run has no
        parent it was discovered under, so it gets the literal placeholder
        "root" rather than a numeric weight.
        """
        if node_id in self.seen_nids:
            self.log_progress(
                f"Node {node_id} already processed; skipping duplicate.",
                level=logging.WARNING,
            )
            return {}
        self.seen_nids.add(node_id)

        nodes = {node_id: weight}
        if not self.config.get("include_member_media", True):
            return nodes

        max_depth = self.config.get("member_media_max_depth")
        if max_depth is not None and depth >= max_depth:
            return nodes

        for member in self.get_member_node_ids(node_id):
            nodes.update(
                self.collect_node_and_members(
                    member["nid"], weight=member["weight"], depth=depth + 1
                )
            )

        return nodes

    def export(self):
        """
        Main entry point, called from the workbench script's
        export_member_media() task function.

        For each starting node_id (from input_csv or a single config
        value), recursively collects that node and its members (each
        with its weight relative to its own parent), downloads every
        media file found on each collected node (all Media Use types)
        into a per-node subfolder of export_file_directory, and writes a
        CSV manifest of every file processed.

        Returns a 5-tuple: (export_file_directory, csv_path,
        total_downloads, successful_downloads, unsuccessful_downloads).
        """
        if self.config.get("export_member_media_report_only", False):
            action_verb, action_verb_past = "find", "found"
        elif self.config.get("export_file_url_instead_of_download", False):
            action_verb, action_verb_past = "list", "listed"
        else:
            action_verb, action_verb_past = "export", "exported"

        starting_node_ids = self._get_starting_node_ids()
        total_downloads = 0
        successful_downloads = 0
        unsuccessful_downloads = 0
        csv_rows = []

        for starting_node_id in starting_node_ids:
            nodes_with_weights = self.collect_node_and_members(starting_node_id)
            self.log_progress(
                f"Node {starting_node_id}: {len(nodes_with_weights)} node(s) to process "
                f"(include_member_media={self.config.get('include_member_media', True)}, "
                f"member_media_max_depth={self.config.get('member_media_max_depth')})."
            )

            for node_id, weight in nodes_with_weights.items():
                file_results = self.process_all_file_data(node_id, weight)
                if not file_results:
                    # No media at all found for this node.
                    total_downloads += 1
                    unsuccessful_downloads += 1
                    continue

                node_successes = 0
                node_failures = 0
                for result in file_results:
                    total_downloads += 1
                    if result["status"] in ("downloaded", "listed", "reported"):
                        node_successes += 1
                        csv_rows.append(
                            {
                                "node_id": node_id,
                                "media_id": result.get("media_id"),
                                "media_use_type": result.get("media_use_type"),
                                "media_index": result.get("media_index"),
                                "file_path": result.get("path"),
                            }
                        )
                    else:
                        node_failures += 1

                successful_downloads += node_successes
                unsuccessful_downloads += node_failures
                # "downloaded"/"failed_*" are the only statuses possible in
                # real-download mode -- report_only/url_instead_of_download
                # never produce a failure status today, so node_failures is
                # always 0 in those modes, but the wording still shouldn't
                # claim something was "exported" when nothing was.
                if node_failures > 0:
                    self.log_progress(
                        f"Node {node_id}: {node_successes} file(s) {action_verb_past}, "
                        f"{node_failures} file(s) failed."
                    )
                else:
                    self.log_progress(f"Node {node_id}: {node_successes} file(s) {action_verb_past}.")

        csv_path = self._write_manifest_csv(csv_rows)

        if unsuccessful_downloads > 0:
            self.log_progress(
                f"export_member_media completed. {total_downloads} file(s) attempted, "
                f"{successful_downloads} {action_verb_past} successfully, "
                f"{unsuccessful_downloads} failed. Manifest written to {csv_path}."
            )
        else:
            self.log_progress(
                f"export_member_media completed. {successful_downloads} file(s) "
                f"{action_verb_past}. Manifest written to {csv_path}."
            )
        return (
            self.config["export_file_directory"],
            csv_path,
            total_downloads,
            successful_downloads,
            unsuccessful_downloads,
        )

    def _get_allowed_media_use_tids(self):
        """
        Resolve export_member_media_use_types (a list of term IDs, names,
        or URIs) into term IDs, once per run rather than once per node.
        Returns None if the setting is unset/empty, meaning "no filter,
        export all Media Use types" (the default).
 
        If the setting IS specified but ANY value fails to resolve (e.g.
        a typo like "Thumbnail" instead of "Thumbnail Image"), this
        fails the run outright -- matching check_input()'s strictness
        during --check exactly, rather than silently continuing with
        fewer types than requested. A partial match is still not what
        was asked for, and warning-then-continuing risks the exact kind
        of quiet under/over-export this whole feature exists to avoid.
        check_input() validates this same resolution ahead of time
        during --check, so this is a safety net for runs that skip
        --check, not the only place this gets caught.
        """
        if not hasattr(self, "_allowed_media_use_tids_cache"):
            use_types = self.config.get("export_member_media_use_types")
            if use_types:
                resolved = resolve_media_use_term_ids(self.config, use_types)
                if len(resolved) < len(use_types):
                    message = (
                        f"One or more values in export_member_media_use_types "
                        f"({use_types}) could not be resolved to a real Media "
                        f"Use term. See the log above for which specific "
                        f"value(s) failed, and confirm the term name/URI/ID is "
                        f"correct."
                    )
                    self.log_progress(message, level=logging.ERROR)
                    # Plain exit code, not the message -- log_progress() above
                    # already printed and logged it; passing a string to
                    # sys.exit() makes Python's own runtime echo it a second
                    # time to stderr, which is what caused the duplicate
                    # "ERROR: ..."/"Error: ..." lines seen in testing.
                    sys.exit(1)
                self._allowed_media_use_tids_cache = resolved
            else:
                self._allowed_media_use_tids_cache = None
        return self._allowed_media_use_tids_cache

    def _resolve_media_use_label_string(self, media_use_type_raw):
        """
        Resolve a comma-joined string of Media Use term IDs (as stored in
        a file entry's "media_use_type", e.g. "16" or "15,16") into a
        filename-safe, human-readable label string (e.g. "Original-File"
        or "Service-File+Original-File"). Term-name lookups are cached on
        the instance (self._media_use_label_cache) since the same handful
        of Media Use terms repeat across every node in a run -- without
        caching, a large run would re-fetch the same term name from
        Drupal hundreds of times.

        Falls back to the raw term ID itself (still sanitized) if a
        term's name can't be resolved, so a single lookup failure doesn't
        blank out or crash the whole filename.
        """
        if not hasattr(self, "_media_use_label_cache"):
            self._media_use_label_cache = {}

        if not media_use_type_raw:
            return "NoMediaUse"

        labels = []
        for tid in media_use_type_raw.split(","):
            tid = tid.strip()
            if tid not in self._media_use_label_cache:
                name = get_term_name(self.config, tid)
                self._media_use_label_cache[tid] = name if name else tid
            labels.append(self._sanitize_label(self._media_use_label_cache[tid]))

        # "+" rather than "," to join multiple labels -- a literal comma
        # in a filename is legal on most filesystems but awkward for
        # shell globbing/scripting downstream.
        return "+".join(labels)

    @staticmethod
    def _sanitize_label(label):
        """
        Make a human-readable term name safe to use as a filename
        component: spaces to hyphens, and strip characters that are
        illegal or awkward across common filesystems.
        """
        label = str(label).strip().replace(" ", "-")
        return re.sub(r'[\\/:*?"<>|,]', "", label)

    def _build_filename(self, node_id, weight, media_id, media_index, media_use_label, extension):
        """
        Build the output filename for a file, per the configured
        export_member_media_filename_format:
 
        "default" (the default): {node_id}-{media_id}-{media_index}-{media_use_label}{extension}
            A thorough, unambiguous dump suited to further programmatic
            processing -- media_id ties each file back to its exact
            Drupal media entity.
 
        "export": {node_id}-{weight}-{media_index}-{media_use_label}{extension}
            weight and media_index are zero-padded to 4 digits (e.g.
            "0003") so that sorting files by filename in the OS/shell
            produces the correct reading order once files from multiple
            nodes are flattened together -- which plain numeric sort
            order would NOT do without padding (e.g. "10" sorting before
            "2"). The root/starting node's weight is the literal string
            "root" (never numeric, so never padded) since it has no
            parent it was discovered under. A member node whose weight
            comes back empty/None/missing -- e.g. a flat collection where
            each item is its own independent node with no page/reading
            order at all, so field_weight is never populated -- gets the
            placeholder "noweight" instead, rather than an empty filename
            segment or a variable filename structure. This keeps every
            "export"-format filename at a consistent 4 segments
            regardless of whether weight data exists for a given node.
        """
        filename_format = self.config.get("export_member_media_filename_format", "default")
        padded_index = str(media_index).zfill(4)
 
        if filename_format == "export":
            if weight and str(weight).isdigit():
                padded_weight = str(weight).zfill(4)
            elif weight == "root":
                padded_weight = "root"
            else:
                # weight is None, empty string, or some other non-numeric,
                # non-"root" value -- use an explicit placeholder rather
                # than an empty string, which would otherwise silently
                # produce a blank segment in the filename
                # (e.g. "1334--0001-Original-File.jp2").
                padded_weight = "noweight"
            return f"{node_id}-{padded_weight}-{padded_index}-{media_use_label}{extension}"
 
        return f"{node_id}-{media_id}-{media_index}-{media_use_label}{extension}"

    def process_all_file_data(self, node_id, weight):
        """
        Resolves and downloads (or lists URLs for, per
        export_file_url_instead_of_download, or just reports on, per
        export_member_media_report_only) EVERY media file attached to a
        node, across all Media Use types unless
        export_member_media_use_types restricts it to a subset. Creates a
        per-node subfolder of export_file_directory for actual downloads
        (export_file_directory/{node_id}/) so files from different nodes
        never collide or get mixed together on disk.

        'weight' is this node's weight relative to its own parent (see
        collect_node_and_members()), used only by the "export" filename
        format.

        media_index (1-based position among files sharing this node_id +
        media_use_type) is computed HERE, per call, rather than by
        export() -- since each node_id is processed in exactly one call,
        the counter naturally scopes correctly without needing to be
        threaded across the whole run.

        Mode precedence: report_only is checked first (since it implies
        "don't download AND don't even resolve a URL commitment"), then
        export_file_url_instead_of_download, then a real download. This
        matters if more than one of these settings is enabled at once.

        Returns a list of {"media_id": str|None, "media_use_type": str|None,
        "media_index": int, "path": str|None, "status": str} dicts, one
        per file ATTEMPTED (not just succeeded). An empty list means no
        matching media was found at all for this node. In report_only
        mode, "path" is always None -- nothing is downloaded, so there is
        no disk path to report, and no URL is resolved either (distinct
        from export_file_url_instead_of_download mode, which does
        populate "path" with the file's URL).
        """
        allowed_media_use_tids = self._get_allowed_media_use_tids()
        file_entries = get_all_media_files_for_node(
            self.config, node_id, allowed_media_use_tids
        )
        if not file_entries:
            return []

        media_index_counters = collections.defaultdict(int)

        if self.config.get("export_member_media_report_only", False):
            node_output_dir = os.path.join(self.config["export_file_directory"], str(node_id))
            results = []
            for entry in file_entries:
                media_index_counters[entry["media_use_type"]] += 1
                media_index = media_index_counters[entry["media_use_type"]]

                # Construct the WOULD-BE path/filename, exactly as a real
                # download would, without creating the directory or
                # downloading anything -- this is a preview, not a write.
                _, extension = os.path.splitext(entry["filename"])
                media_use_label = self._resolve_media_use_label_string(entry["media_use_type"])
                predicted_filename = self._build_filename(
                    node_id, weight, entry["media_id"], media_index, media_use_label, extension
                )
                predicted_path = os.path.join(node_output_dir, predicted_filename)
                # Best-effort dedup preview: only accurate if the directory's
                # contents don't change between this report_only run and a
                # later real run.
                if os.path.exists(predicted_path):
                    predicted_path = get_deduped_file_path(predicted_path)

                results.append(
                    {
                        "media_id": entry["media_id"],
                        "media_use_type": entry["media_use_type"],
                        "media_index": media_index,
                        "path": predicted_path,
                        "status": "reported",
                    }
                )
            return results

        if self.config.get("export_file_url_instead_of_download", False):
            results = []
            for entry in file_entries:
                media_index_counters[entry["media_use_type"]] += 1
                results.append(
                    {
                        "media_id": entry["media_id"],
                        "media_use_type": entry["media_use_type"],
                        "media_index": media_index_counters[entry["media_use_type"]],
                        # Listing a URL can't itself fail the way a download
                        # can, so every entry found is reported as "listed".
                        "path": entry["url"],
                        "status": "listed",
                    }
                )
            return results

        node_output_dir = os.path.join(self.config["export_file_directory"], str(node_id))
        os.makedirs(node_output_dir, exist_ok=True)

        results = []
        for entry in file_entries:
            media_index_counters[entry["media_use_type"]] += 1
            media_index = media_index_counters[entry["media_use_type"]]

            _, extension = os.path.splitext(entry["filename"])
            media_use_label = self._resolve_media_use_label_string(entry["media_use_type"])
            target_filename = self._build_filename(
                node_id, weight, entry["media_id"], media_index, media_use_label, extension
            )
            target_path = os.path.join(node_output_dir, target_filename)
            if os.path.exists(target_path):
                target_path = get_deduped_file_path(target_path)

            download_result = download_file_by_url(self.config, entry["url"], target_path)
            results.append(
                {
                    "media_id": entry["media_id"],
                    "media_use_type": entry["media_use_type"],
                    "media_index": media_index,
                    "path": download_result["path"],
                    "status": download_result["status"],
                }
            )

        return results

    def _write_manifest_csv(self, csv_rows):
        """
        Write the CSV manifest of every file processed (node_id, media_id,
        media_use_type, media_index, file_path). Path comes from
        export_member_media_csv_file_path if set, otherwise defaults to
        export_file_directory/media_manifest.csv.
        """
        csv_path = self.config.get("export_member_media_csv_file_path") or os.path.join(
            self.config["export_file_directory"], "media_manifest.csv"
        )
        fieldnames = ["node_id", "media_id", "media_use_type", "media_index", "file_path"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in csv_rows:
                writer.writerow(row)
        return csv_path

    def _get_starting_node_ids(self):
        """
        Read the starting node ID(s) for this task: either config["node_id"]
        (a single value) or every node_id in config["input_csv"]. Mirrors
        the required-key check already validated in check_input()'s
        "export_member_media" branch, so this method assumes at least one
        of the two is present and well-formed by the time export() runs.
        """
        if "node_id" in self.config and self.config["node_id"]:
            return [str(self.config["node_id"])]

        node_ids = []
        for row in get_csv_data(self.config):
            if value_is_numeric(row.get("node_id")):
                node_ids.append(str(row["node_id"]))
        return node_ids