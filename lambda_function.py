import requests
from icalendar import Calendar, Event
from datetime import date, datetime, time, timedelta
import pytz
import urllib.parse

# User-configurable variables
CALENDAR_NAME = "Sun Knudsen"  # Set calendar name
USER_TIMEZONE = "America/Montreal"  # Set timezone
WORKWEEK_DAYS = {0, 1, 2, 3, 4}  # Set workweek days (Monday=0, Tuesday=1, …, Friday=4)
START_TIME = time(9, 0)  # Set start time of workweek days (9:00 AM)
END_TIME = time(17, 0)  # Set end time of workweek days (5:00 PM)
ALL_DAY = False  # Set to True to include all events regardless of start and end time
DAYS_IN_PAST = 7  # Set number of days in the past to include in combined calendar
EVENT_NAME = "Busy"  # Set name for events
EXCLUDED_FIELDS = {"ATTENDEE", "DESCRIPTION", "LOCATION", "ORGANIZER"}  # Set private data to exclude
ENABLE_DEBUG = False  # Set to True to enable debugging

TZ = pytz.timezone(USER_TIMEZONE)  # Set timezone
CUTOFF_DATE = datetime.now(TZ) - timedelta(days=DAYS_IN_PAST)  # Set cutoff date

def debug_print(message):
  """
  Print debug messages if debugging is enabled.
  """
  if ENABLE_DEBUG:
    print(f"[DEBUG] {message}")

def adjust_google_calendar_url(url, cutoff_date):
  """
  Adjust Google Calendar URLs to include a ?start-min parameter with the given cutoff date.
  """
  cutoff_date_iso = cutoff_date.strftime("%Y-%m-%dT%H:%M:%SZ")  # Format cutoff date in ISO format
  parsed_url = urllib.parse.urlparse(url)
  query_params = urllib.parse.parse_qs(parsed_url.query)
  query_params["start-min"] = [cutoff_date_iso]  # Add or update the `start-min` parameter
  new_query = urllib.parse.urlencode(query_params, doseq=True)
  updated_url = urllib.parse.urlunparse(
    parsed_url._replace(query=new_query)
  )
  debug_print(f"Adjusted Google Calendar URL: {updated_url}")
  return updated_url

def make_datetime(dt):
  """
  Convert a date or naive datetime to an aware datetime using the user-defined timezone.
  Handles timezones explicitly specified in the calendar event.
  """
  if dt is None:
    raise ValueError("DTSTART or DTEND is missing.")

  if hasattr(dt, "dt"):
    dt = dt.dt

  if isinstance(dt, datetime):
    if dt.tzinfo:  # If already timezone-aware, convert to the user timezone
      return dt.astimezone(TZ)
    else:  # Localize naive datetime to user-defined timezone
      return TZ.localize(dt)
  elif isinstance(dt, date):
    return TZ.localize(datetime.combine(dt, time.min))  # Convert date to datetime at midnight in user timezone

  raise ValueError(f"Invalid datetime object passed to make_datetime: {type(dt)}")

def adjust_to_work_hours(event_start, event_end):
  """
  Adjust event times to fit within the workday range (user-defined timezone).
  Handles multi-day events and ensures only the portion within the workday is included.
  """
  # Initialize variables for adjusted start and end times
  adjusted_start = None
  adjusted_end = None

  if ALL_DAY:
    start_time = time(0, 0)
    end_time = time(23, 59, 59)
  else:
    start_time = START_TIME
    end_time = END_TIME

  # If the event starts on a non-workday, move to the next workday’s start_time
  if not is_work_week_day(event_start):
    event_start = TZ.localize(datetime.combine(event_start.date() + timedelta(days=1), start_time))

  # Define workday boundaries for the start and end dates
  workday_start = TZ.localize(datetime.combine(event_start.date(), start_time))
  workday_end = TZ.localize(datetime.combine(event_start.date(), end_time))

  # If the event ends on a non-workday, truncate to the nearest prior workday’s end_time
  if not is_work_week_day(event_end):
    event_end = TZ.localize(datetime.combine(event_end.date() - timedelta(days=1), end_time))

  # Case 1: Event starts and ends on the same workday
  if event_start.date() == event_end.date():
    adjusted_start = max(event_start, workday_start)
    adjusted_end = min(event_end, workday_end)

  # Case 2: Event starts before workday hours and spans into a workday
  elif event_start < workday_start and event_end > workday_start:
    adjusted_start = workday_start
    adjusted_end = min(event_end, workday_end)

  # Case 3: Event spans multiple days; truncate to workday hours
  elif event_start < workday_start and event_end.date() > event_start.date():
    adjusted_start = workday_start
    adjusted_end = workday_end

  # Exclude events with zero or negative duration after adjustment
  if adjusted_start and adjusted_end and adjusted_start >= adjusted_end:
    return None, None

  return adjusted_start, adjusted_end

def is_work_week_day(event_start):
  """
  Check if the event start date falls within the defined workweek days.
  """
  return event_start.weekday() in WORKWEEK_DAYS

def adjust_recurring_event_base(event):
  """
  Adjust the start and end times of the base recurring event to fit work hours,
  and exclude events that have ended before now.
  """
  try:
    event_start = make_datetime(event.get("DTSTART"))
    event_end = make_datetime(event.get("DTEND"))

    # Check RRULE for the UNTIL parameter
    rrule = event.get("RRULE")
    if rrule and "UNTIL" in rrule:
      until_date = make_datetime(rrule["UNTIL"][0])
      if until_date < datetime.now(TZ):
        debug_print(f"Recurring event has ended (UNTIL={until_date}). Excluding event.")
        return None  # Exclude the event

    # Adjust the event’s end time based on its start time and duration
    adjusted_start, adjusted_end = adjust_to_work_hours(event_start, event_end)
    if adjusted_start and adjusted_end:
      # Update the event’s start and end times
      event["DTSTART"] = adjusted_start
      event["DTEND"] = adjusted_end
      debug_print(f"Adjusted recurring event: {adjusted_start} to {adjusted_end}")
    else:
      debug_print(f"Excluded recurring event outside work hours: {event_start} to {event_end}")
      return None  # Exclude the event if it’s outside work hours
  except Exception as e:
    debug_print(f"Exception occurred while adjusting recurring event: {e}")
    return None  # Exclude the event in case of errors

  return event

def lambda_handler(event, context):
  urls = [url.strip() for url in open("urls.txt").readlines()]

  try:
    combined_cal = Calendar()
    combined_cal.add("PRODID", "-//Sun Knudsen//Combined shared calendar 1.0//EN")
    combined_cal.add("VERSION", "2.0")
    combined_cal.add("X-WR-CALNAME", CALENDAR_NAME)

    if not urls:
      debug_print("No URLs found in urls.txt.")
      return {"statusCode": 400, "body": "No URLs provided"}

    for url in urls:
      # Only adjust Google Calendar URLs
      if url.startswith("https://calendar.google.com"):
        url = adjust_google_calendar_url(url, CUTOFF_DATE)

      debug_print(f"Fetching calendar from: {url}")
      req = requests.get(url)

      if req.status_code != 200:
        debug_print(f"Error fetching {url}: {req.status_code}, {req.text}")
        continue

      debug_print(f"Successfully fetched calendar from: {url}")
      cal = Calendar.from_ical(req.text)

      for event in cal.walk("VEVENT"):
        event_start = event.get("DTSTART")
        event_end = event.get("DTEND")

        if event_start and event_end:
          event_start = make_datetime(event_start)
          event_end = make_datetime(event_end)

          # Exclude non-recurring past events
          if event_start < CUTOFF_DATE and "RRULE" not in event:
            debug_print(f"Skipping non-recurring past event: {event_start}")
            continue

          # Adjust event times to fit within work hours
          adjusted_start, adjusted_end = adjust_to_work_hours(event_start, event_end)
          if not adjusted_start or not adjusted_end:
            debug_print(f"Skipping event outside work hours: {event_start} to {event_end}")
            continue

          # Handle recurring events
          if "RRULE" in event:
            adjusted_event = adjust_recurring_event_base(event)
            if not adjusted_event:
              debug_print(f"Skipping invalid or ended recurring event: {event}")
              continue
            adjusted_start = adjusted_event.get("DTSTART")
            adjusted_end = adjusted_event.get("DTEND")
          else:
            adjusted_event = event

          # Use adjusted times explicitly
          copied_event = Event()
          copied_event.add("DTSTART", adjusted_start)
          copied_event.add("DTEND", adjusted_end)

          # Add non-private attributes, excluding sensitive fields
          for attr in event:
            if attr.upper() not in EXCLUDED_FIELDS and attr not in {"DTSTART", "DTEND"}:
              if isinstance(event[attr], list):
                for element in event[attr]:
                  copied_event.add(attr, element)
              else:
                copied_event.add(attr, event[attr])

          copied_event["SUMMARY"] = EVENT_NAME
          combined_cal.add_component(copied_event)

    # Write the combined calendar to an ICS file locally when simulating runs
    if len(combined_cal.subcomponents) > 0:
      if context is None:  # Simulated execution
        with open("test.ics", "wb") as f:
          f.write(combined_cal.to_ical())
        debug_print("Combined calendar written to test.ics")
    else:
      debug_print("No events found to include in the combined calendar.")

    return {
      "statusCode": 200,
      "headers": {
        "Content-Type": "text/calendar"
      },
      "body": combined_cal.to_ical().decode("utf-8") if combined_cal.subcomponents else "No events found"
    }

  except Exception as e:
    debug_print(f"Exception occurred: {e}")
    return {
      "statusCode": 500,
      "body": f"An error occurred: {e}"
    }

if __name__ == "__main__":
  test_event = {}
  test_context = None
  response = lambda_handler(test_event, test_context)
