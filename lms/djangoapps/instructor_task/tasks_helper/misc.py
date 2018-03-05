"""
This file contains tasks that are designed to perform background operations on the
running state of a course.

"""
import csv
import logging
from collections import OrderedDict
from datetime import datetime
from StringIO import StringIO
from time import time

import unicodecsv
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.storage import DefaultStorage
from openassessment.data import OraAggregateData
from pytz import UTC

from courseware.courses import get_course_by_id
from instructor_analytics.basic import get_proctored_exam_results
from instructor_analytics.csvs import format_dictlist
from instructor.views.instructor_task_helpers import (
    do_remote_gradebook,
    get_assignment_grade_datatable,
)
from openedx.core.djangoapps.course_groups.cohorts import add_user_to_cohort
from openedx.core.djangoapps.course_groups.models import CourseUserGroup
from survey.models import SurveyAnswer
from util.file import UniversalNewlineIterator, course_filename_prefix_generator

from .runner import TaskProgress
from .utils import UPDATE_STATUS_FAILED, UPDATE_STATUS_SUCCEEDED, upload_csv_to_report_store

# define different loggers for use within tasks and on client side
TASK_LOG = logging.getLogger('edx.celery.task')


def upload_course_survey_report(_xmodule_instance_args, _entry_id, course_id, _task_input, action_name):
    """
    For a given `course_id`, generate a html report containing the survey results for a course.
    """
    start_time = time()
    start_date = datetime.now(UTC)
    num_reports = 1
    task_progress = TaskProgress(action_name, num_reports, start_time)

    current_step = {'step': 'Gathering course survey report information'}
    task_progress.update_task_state(extra_meta=current_step)

    distinct_survey_fields_queryset = SurveyAnswer.objects.filter(course_key=course_id).values('field_name').distinct()
    survey_fields = []
    for unique_field_row in distinct_survey_fields_queryset:
        survey_fields.append(unique_field_row['field_name'])
    survey_fields.sort()

    user_survey_answers = OrderedDict()
    survey_answers_for_course = SurveyAnswer.objects.filter(course_key=course_id).select_related('user')

    for survey_field_record in survey_answers_for_course:
        user_id = survey_field_record.user.id
        if user_id not in user_survey_answers.keys():
            user_survey_answers[user_id] = {
                'username': survey_field_record.user.username,
                'email': survey_field_record.user.email
            }

        user_survey_answers[user_id][survey_field_record.field_name] = survey_field_record.field_value

    header = ["User ID", "User Name", "Email"]
    header.extend(survey_fields)
    csv_rows = []

    for user_id in user_survey_answers.keys():
        row = []
        row.append(user_id)
        row.append(user_survey_answers[user_id].get('username', ''))
        row.append(user_survey_answers[user_id].get('email', ''))
        for survey_field in survey_fields:
            row.append(user_survey_answers[user_id].get(survey_field, ''))
        csv_rows.append(row)

    task_progress.attempted = task_progress.succeeded = len(csv_rows)
    task_progress.skipped = task_progress.total - task_progress.attempted

    csv_rows.insert(0, header)

    current_step = {'step': 'Uploading CSV'}
    task_progress.update_task_state(extra_meta=current_step)

    # Perform the upload
    upload_csv_to_report_store(csv_rows, 'course_survey_results', course_id, start_date)

    return task_progress.update_task_state(extra_meta=current_step)


def upload_proctored_exam_results_report(_xmodule_instance_args, _entry_id, course_id, _task_input, action_name):  # pylint: disable=invalid-name
    """
    For a given `course_id`, generate a CSV file containing
    information about proctored exam results, and store using a `ReportStore`.
    """
    start_time = time()
    start_date = datetime.now(UTC)
    num_reports = 1
    task_progress = TaskProgress(action_name, num_reports, start_time)
    current_step = {'step': 'Calculating info about proctored exam results in a course'}
    task_progress.update_task_state(extra_meta=current_step)

    # Compute result table and format it
    query_features = _task_input.get('features')
    student_data = get_proctored_exam_results(course_id, query_features)
    header, rows = format_dictlist(student_data, query_features)

    task_progress.attempted = task_progress.succeeded = len(rows)
    task_progress.skipped = task_progress.total - task_progress.attempted

    rows.insert(0, header)

    current_step = {'step': 'Uploading CSV'}
    task_progress.update_task_state(extra_meta=current_step)

    # Perform the upload
    upload_csv_to_report_store(rows, 'proctored_exam_results_report', course_id, start_date)

    return task_progress.update_task_state(extra_meta=current_step)


def cohort_students_and_upload(_xmodule_instance_args, _entry_id, course_id, task_input, action_name):
    """
    Within a given course, cohort students in bulk, then upload the results
    using a `ReportStore`.
    """
    start_time = time()
    start_date = datetime.now(UTC)

    # Iterate through rows to get total assignments for task progress
    with DefaultStorage().open(task_input['file_name']) as f:
        total_assignments = 0
        for _line in unicodecsv.DictReader(UniversalNewlineIterator(f)):
            total_assignments += 1

    task_progress = TaskProgress(action_name, total_assignments, start_time)
    current_step = {'step': 'Cohorting Students'}
    task_progress.update_task_state(extra_meta=current_step)

    # cohorts_status is a mapping from cohort_name to metadata about
    # that cohort.  The metadata will include information about users
    # successfully added to the cohort, users not found, Preassigned
    # users, and a cached reference to the corresponding cohort object
    # to prevent redundant cohort queries.
    cohorts_status = {}

    with DefaultStorage().open(task_input['file_name']) as f:
        for row in unicodecsv.DictReader(UniversalNewlineIterator(f), encoding='utf-8'):
            # Try to use the 'email' field to identify the user.  If it's not present, use 'username'.
            username_or_email = row.get('email') or row.get('username')
            cohort_name = row.get('cohort') or ''
            task_progress.attempted += 1

            if not cohorts_status.get(cohort_name):
                cohorts_status[cohort_name] = {
                    'Cohort Name': cohort_name,
                    'Learners Added': 0,
                    'Learners Not Found': set(),
                    'Invalid Email Addresses': set(),
                    'Preassigned Learners': set()
                }
                try:
                    cohorts_status[cohort_name]['cohort'] = CourseUserGroup.objects.get(
                        course_id=course_id,
                        group_type=CourseUserGroup.COHORT,
                        name=cohort_name
                    )
                    cohorts_status[cohort_name]["Exists"] = True
                except CourseUserGroup.DoesNotExist:
                    cohorts_status[cohort_name]["Exists"] = False

            if not cohorts_status[cohort_name]['Exists']:
                task_progress.failed += 1
                continue

            try:
                # If add_user_to_cohort successfully adds a user, a user object is returned.
                # If a user is preassigned to a cohort, no user object is returned (we already have the email address).
                (user, previous_cohort, preassigned) = add_user_to_cohort(cohorts_status[cohort_name]['cohort'], username_or_email)
                if preassigned:
                    cohorts_status[cohort_name]['Preassigned Learners'].add(username_or_email)
                    task_progress.preassigned += 1
                else:
                    cohorts_status[cohort_name]['Learners Added'] += 1
                    task_progress.succeeded += 1
            except User.DoesNotExist:
                # Raised when a user with the username could not be found, and the email is not valid
                cohorts_status[cohort_name]['Learners Not Found'].add(username_or_email)
                task_progress.failed += 1
            except ValidationError:
                # Raised when a user with the username could not be found, and the email is not valid,
                # but the entered string contains an "@"
                # Since there is no way to know if the entered string is an invalid username or an invalid email,
                # assume that a string with the "@" symbol in it is an attempt at entering an email
                cohorts_status[cohort_name]['Invalid Email Addresses'].add(username_or_email)
                task_progress.failed += 1
            except ValueError:
                # Raised when the user is already in the given cohort
                task_progress.skipped += 1

            task_progress.update_task_state(extra_meta=current_step)

    current_step['step'] = 'Uploading CSV'
    task_progress.update_task_state(extra_meta=current_step)

    # Filter the output of `add_users_to_cohorts` in order to upload the result.
    output_header = ['Cohort Name', 'Exists', 'Learners Added', 'Learners Not Found', 'Invalid Email Addresses', 'Preassigned Learners']
    output_rows = [
        [
            ','.join(status_dict.get(column_name, '')) if (column_name == 'Learners Not Found'
                                                           or column_name == 'Invalid Email Addresses'
                                                           or column_name == 'Preassigned Learners')
            else status_dict[column_name]
            for column_name in output_header
        ]
        for _cohort_name, status_dict in cohorts_status.iteritems()
    ]
    output_rows.insert(0, output_header)
    upload_csv_to_report_store(output_rows, 'cohort_results', course_id, start_date)

    return task_progress.update_task_state(extra_meta=current_step)


def upload_ora2_data(
        _xmodule_instance_args, _entry_id, course_id, _task_input, action_name
):
    """
    Collect ora2 responses and upload them to S3 as a CSV
    """

    start_date = datetime.now(UTC)
    start_time = time()

    num_attempted = 1
    num_total = 1

    fmt = u'Task: {task_id}, InstructorTask ID: {entry_id}, Course: {course_id}, Input: {task_input}'
    task_info_string = fmt.format(
        task_id=_xmodule_instance_args.get('task_id') if _xmodule_instance_args is not None else None,
        entry_id=_entry_id,
        course_id=course_id,
        task_input=_task_input
    )
    TASK_LOG.info(u'%s, Task type: %s, Starting task execution', task_info_string, action_name)

    task_progress = TaskProgress(action_name, num_total, start_time)
    task_progress.attempted = num_attempted

    curr_step = {'step': "Collecting responses"}
    TASK_LOG.info(
        u'%s, Task type: %s, Current step: %s for all submissions',
        task_info_string,
        action_name,
        curr_step,
    )

    task_progress.update_task_state(extra_meta=curr_step)

    try:
        header, datarows = OraAggregateData.collect_ora2_data(course_id)
        rows = [header] + [row for row in datarows]
    # Update progress to failed regardless of error type
    except Exception:  # pylint: disable=broad-except
        TASK_LOG.exception('Failed to get ORA data.')
        task_progress.failed = 1
        curr_step = {'step': "Error while collecting data"}

        task_progress.update_task_state(extra_meta=curr_step)

        return UPDATE_STATUS_FAILED

    task_progress.succeeded = 1
    curr_step = {'step': "Uploading CSV"}
    TASK_LOG.info(
        u'%s, Task type: %s, Current step: %s',
        task_info_string,
        action_name,
        curr_step,
    )
    task_progress.update_task_state(extra_meta=curr_step)

    upload_csv_to_report_store(rows, 'ORA_data', course_id, start_date)

    curr_step = {'step': 'Finalizing ORA data report'}
    task_progress.update_task_state(extra_meta=curr_step)
    TASK_LOG.info(u'%s, Task type: %s, Upload complete.', task_info_string, action_name)

    return UPDATE_STATUS_SUCCEEDED

def generate_assignment_grade_csv(_xmodule_instance_args, _entry_id, course_id, task_input, action_name):
    start_time = time()
    start_date = datetime.now(UTC)
    num_reports = 1
    task_progress = TaskProgress(action_name, num_reports, start_time)

    if not task_input['assignment_name']:
        return _progress_error("Error, assignment name missing", task_progress)

    current_step = {'step': 'Get course from modulestore'}
    task_progress.update_task_state(extra_meta=current_step)
    course = get_course_by_id(course_id)

    current_step = {'step': 'Load grades'}
    task_progress.update_task_state(extra_meta=current_step)
    __, data_table = get_assignment_grade_datatable(course, task_input['assignment_name'])

    rows = data_table["data"]
    task_progress.attempted = task_progress.succeeded = len(rows)
    task_progress.skipped = task_progress.total - task_progress.attempted
    rows.insert(0, data_table["header"])
    current_step = {'step': 'Uploading CSV'}
    task_progress.update_task_state(extra_meta=current_step)

    # Perform the upload
    upload_csv_to_report_store(rows, 'grades', course_id, start_date)

    current_step = {'step': 'Uploaded CSV'}
    return task_progress.update_task_state(extra_meta=current_step)


def post_grades_to_rgb(_xmodule_instance_args, _entry_id, course_id, task_input, action_name):
    start_time = time()
    num_reports = 1
    task_progress = TaskProgress(action_name, num_reports, start_time)

    if not task_input['assignment_name']:
        return _progress_error("Error, assignment name missing", task_progress)

    current_step = {'step': 'Get course from modulestore'}
    task_progress.update_task_state(extra_meta=current_step)
    course = get_course_by_id(course_id)

    current_step = {'step': 'Load grades'}
    task_progress.update_task_state(extra_meta=current_step)
    __, data_table = get_assignment_grade_datatable(course, task_input['assignment_name'])

    task_progress.attempted = task_progress.succeeded = len(data_table["data"])
    task_progress.skipped = task_progress.total - task_progress.attempted

    current_step = {'step': 'Uploading CSV'}
    task_progress.update_task_state(extra_meta=current_step)

    # Perform the upload
    file_pointer = StringIO()
    create_datatable_csv(file_pointer, data_table)
    file_pointer.seek(0)
    files = {'datafile': file_pointer}

    error_message, __ = do_remote_gradebook(
        task_input['email_id'],
        course,
        'post-grades',
        files=files,
    )

    if error_message:
        return _progress_error(error_message, task_progress)

    current_step = {
        'step': 'Posted to RGB'
    }
    return task_progress.update_task_state(extra_meta=current_step)


def create_datatable_csv(csv_file, datatable):
    """Creates a CSV file from the contents of a datatable."""
    writer = csv.writer(csv_file, dialect='excel', quotechar='"', quoting=csv.QUOTE_ALL)
    encoded_row = [unicode(s).encode('utf-8') for s in datatable['header']]
    writer.writerow(encoded_row)
    for datarow in datatable['data']:
        # 's' here may be an integer, float (eg score) or string (eg student name)
        encoded_row = [
            # If s is already a UTF-8 string, trying to make a unicode
            # object out of it will fail unless we pass in an encoding to
            # the constructor. But we can't do that across the board,
            # because s is often a numeric type. So just do this.
            s if isinstance(s, str) else unicode(s).encode('utf-8')
            for s in datarow
        ]
        writer.writerow(encoded_row)
    return csv_file


def _progress_error(error_msg, task_progress):
    task_progress.failed = 1
    curr_step = {'step': error_msg}
    task_progress.update_task_state(extra_meta=curr_step)
    return UPDATE_STATUS_FAILED
