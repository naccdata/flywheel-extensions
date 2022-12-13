"""Reads a YAML file with project info.

project - name of project
centers - array of centers
    center-id - "ADC" ID of center (protected info)
    name - name of center
    is-active - whether center is active, has users if True
datatypes - array of datatype names (form, dicom)
published - boolean indicating whether data is to be published
"""
import argparse
import logging
from pathvalidate import ValidationError, validate_filename, sanitize_filename
import sys
from typing import Optional

import yaml
import flywheel
from flywheel import ApiException

from common.src.projects.project import Center, Project, ProjectVisitor, convert_to_slug


DRYRUN = True


def sanitize_name(name: str, groupid: bool = False) -> str:
    """ Sanitizes a name for flywheel

    Flywheel name/label requirments are that it must be less than 64 characters,
    and group ids can include lowercase letters, numbers, dashes, and underscores as long as it’s unique.

    Args:
        name: the name to be sanitized
        groupid: boolean indicating if the name is a group ID or not (which has special sanitation rules)

    Returns:
        safe_name: the new sanitized name for the container/group that's safe for flywheel

    """

    safe_name = ""
    if groupid:
        # Group ID can include lowercase letters, numbers, dashes, and underscores as long as it’s unique.
        modname = name.lower() # Convert to lowercase
        modname = modname.replace(" ", "_") # Replace spaces with dashes
        for c in modname:
            safe_name += c if c.isalnum() or c in ['-','_'] else ""

    else:
        safe_name = name[:64]

    if safe_name != name:
        logging.info(f"changed from {name} to {safe_name}")

    return safe_name

def flywheel_path_exists(fwpath: str, fw: flywheel.Client) -> bool:
    """ ensure that a fw path (group, or group/project) is valid for creation.
    i.e., ensure that it doesn't exist already

    Args:
        fwpath: a string path to a group, or group/project (just "<groupid>", or "<groupid>/<project>")
        fw: flywheel Client

    Returns: True|False

    """

    try:
        fw.lookup(fwpath)
    except ApiException as e:
        if e.status == 404:
            return False
    return True


def create_flywheel_group(*, group_label: str, group_id: str, fw: flywheel.Client) -> str:
    """Creates FW group with label and ID.

    Args:
      group_label: the name of the project to be created
      group_id: the id for the group
      fw: flywheel sdk Client
    Returns:
      the ID of the FW group
    """
    group_label = sanitize_name(group_label)
    group_id = sanitize_name(group_id, groupid=True)

    if flywheel_path_exists(group_id):
        logging.info(f"Flywheel group {group_id} already exists")
        return group_id

    logging.info("Creating group")
    logging.info("  group label: %s", group_label)
    logging.info("  group ID: %s", group_id)

    if DRYRUN:
        return group_id

    group_id = fw.add_group(flywheel.Group(group_id, group_label))
    logging.info("\tsuccess")

    return group_id



def create_flywheel_project(*, group_id: str, project_id: str,
                            project_label: str, fw: flywheel.Client) -> str:

    """Creates FW project w/in group with given name.

    Args:
      group_id: the group
      project_id: the name of the project
      project_label: the display name of the project
      fw: the flywheel SDK client
    """

    project_label = sanitize_name(project_label)

    project_path = f"{group_id}/{project_id}"
    project_ref = f"fw://{project_path}"

    if flywheel_path_exists(project_path):
        logging.info(f"Flywheel group {project_ref} already exists")
        return project_ref

    logging.info("Creating project")
    logging.info("  project: %s", project_ref)
    logging.info("  project name: %s", project_label)

    if DRYRUN:
        return project_ref

    group = fw.get_group(group_id)
    project = group.add_project(label=project_label)

    return project_ref


def create_release(project: Project):
    """Creates a release FW group for the given project with a master FW
    project.

    Args:
        project: the project
    """
    group_id = create_flywheel_group(group_label=project.name + " Release",
                                     group_id="release-" +
                                     convert_to_slug(project.name))

    create_flywheel_project(group_id=group_id,
                            project_id="master-project",
                            project_label="Master Project")


class FlywheelProjectArtifactCreator(ProjectVisitor):
    """Creates project artifacts in Flywheel."""

    def __init__(self) -> None:
        """Inititializes visitor with FW instance details."""
        self.__current_project: Optional[Project] = None

    def __create_accepted(self, group_id: str) -> None:
        """Creates an accepted project for current project within given group.

        Args:
          group_id: the ID for parent group of project
        """
        assert self.__current_project
        project_id = self.__build_project_id("accepted")
        create_flywheel_project(group_id=group_id,
                                project_id=project_id,
                                project_label=self.__current_project.name +
                                " Accepted")

    def __build_project_id(self, prefix: str) -> str:
        """Builds a FW project ID string from the given prefix.

        Concatenates the name of the current project, if is not the primary
        project of the coordinating center.

        Args:
          prefix: the prefix for the project ID
        """
        assert self.__current_project
        if self.__current_project.is_primary():
            return prefix
        return prefix + "-" + convert_to_slug(self.__current_project.name)

    def __create_ingest(self, group_id: str) -> None:
        """Creates an ingest project for current project within the given group
        for each data type in the project.

        Args:
          group_id: the ID for the parent group of the ingest projects.
        """
        assert self.__current_project
        for datatype in self.__current_project.datatypes:
            project_id = self.__build_project_id("ingest-" + datatype.lower())
            create_flywheel_project(group_id=group_id,
                                    project_id=project_id,
                                    project_label=self.__current_project.name +
                                    " " + datatype.capitalize() + " Ingest")

    def visit_center(self, center: Center) -> None:
        """Creates center specific details for project in FW instance.

        Adds a FW group for the center containing
        - one FW project per project and datatype, if center is active
        - one FW project for "accepted" data

        Args:
          center: the Center
        """
        if not self.__current_project:
            logging.error("No project given")
            return

        group_id = create_flywheel_group(group_label=center.name,
                                         group_id=convert_to_slug(center.name))

        if center.is_active():
            if self.__current_project.datatypes:
                self.__create_ingest(group_id)
            else:
                logging.warning(
                    "No ingest groups created for %s: no datatypes given",
                    self.__current_project.name)
        else:
            logging.info("Not creating ingest for inactive center %s",
                         center.name)

        self.__create_accepted(group_id)

    def visit_project(self, project: Project):
        """Creates groups in FW instance:

        - one ingest groups for each datatype for each center
        - one "accepted" groups for each center
        - "release" group for project if project.published
        """
        self.__current_project = project

        if self.__current_project.centers:
            for center in self.__current_project.centers:
                center.apply(self)
        else:
            logging.warning(
                "Not creating center groups for project %s: no centers given",
                self.__current_project.name)

        if self.__current_project.is_published():
            create_release(self.__current_project)
        else:
            logging.info("Project %s has no release project",
                         self.__current_project.name)

        self.__current_project = None

    def visit_datatype(self, datatype: str):
        pass


def main():
    """Main method to create project from the adrc_program.yaml file."""

    logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Create FW structures for Project")
    parser.add_argument('filename')
    args = parser.parse_args()
    project_file = args.filename
    with open(project_file, 'r', encoding='utf-8') as stream:
        try:
            project_gen = yaml.safe_load_all(stream)
        except yaml.YAMLError as exception:
            logging.error("Error in YAML file: %s", project_file)
            if hasattr(exception, 'problem_mark'):
                mark = exception.problem_mark
                logging.error("Error: line %s, column %s", mark.line + 1,
                              mark.column + 1)
            sys.exit(1)
        else:
            project_list = list(project_gen)

    visitor = FlywheelProjectArtifactCreator()
    for project_doc in project_list:
        project = Project.create(project_doc)
        project.apply(visitor)


if __name__ == "__main__":
    main()
