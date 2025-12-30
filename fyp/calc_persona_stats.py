
import math
import re
from datetime import datetime, timedelta
from collections import defaultdict
import regex
from os import listdir
import json
from os.path import join, getmtime
import pandas as pd
from collections import Counter
import numpy as np
from copy import deepcopy




# Helper: Parse a date string ("YYYY-MM-DD HH:MM:SS") into a datetime object.
# Note: The original string is assumed to be in UTC and the function adds 10 hours.
def parse_date(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    # add 10 hours (adjusting from UTC to local time, e.g. Brisbane)
    dt += timedelta(hours=10)
    return dt

# Helper: Compute ISO week number using Python's isocalendar.
def get_week_number(dt):
    # dt.isocalendar() returns a tuple (year, week_number, weekday)
    return dt.isocalendar()[1]

# Helper: Format total viewing time from seconds into a human-readable string.
def format_total_time(total_seconds):
    if total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    total_minutes = total_seconds // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    days = hours // 24
    hours = hours % 24
    parts = []
    if days > 0:
        parts.append(f"{days} day" if days == 1 else f"{days} days")
    if hours > 0:
        parts.append(f"{hours} hour" if hours == 1 else f"{hours} hours")
    if minutes > 0:
        parts.append(f"{minutes} minute" if minutes == 1 else f"{minutes} minutes")
    return ", ".join(parts)



# Helper: Get the most frequently occurring emoji in comments.
# Returns a tuple (most_frequent_emoji, count, total_character_count)
def get_most_frequent_emoji(comments):
    if not comments:
        return (None, None, None)

    # Concatenate all comment texts
    all_comments = "".join(item.get("comment", "") for item in comments)
    total_char_count = len(all_comments)
    # Use regex with Unicode property escape to match emoji characters.
    emoji_matches = regex.findall(r"\p{Emoji}", all_comments)
    
    if not emoji_matches:
        return ("no emoji", 0, total_char_count)
    
    emoji_freq = {}
    for emoji in emoji_matches:
        emoji_freq[emoji] = emoji_freq.get(emoji, 0) + 1
        
    most_freq = None
    max_count = 0
    for emj, count in emoji_freq.items():
        if count > max_count:
            max_count = count
            most_freq = emj
    return (most_freq, max_count, total_char_count)

# Helper: Sum Likes from posts.
def sum_post_likes(posts):
    total = 0
    for post in posts:
        likes_str = post.get("likes", "0")
        try:
            likes = int(likes_str)
        except (ValueError, TypeError):
            likes = 0
        total += likes
    return total

# Helper: Aggregate video views from videos (tallying occurrences by Link).
def aggregate_video_views(videos):
    view_counts = {}
    for video in videos:
        link = video.get("link")
        if link:
            view_counts[link] = view_counts.get(link, 0) + 1
    return view_counts

# Helper: Return the video link with the highest view count.
def get_most_viewed_video_link(video_view_counts):
    max_link = None
    max_count = 0
    for link, count in video_view_counts.items():
        if count > max_count:
            max_count = count
            max_link = link
    return max_link

# Helper: Get the maximum view count from video view counts.
def get_most_viewed_video_counts(video_view_counts):
    max_count = 0
    for count in video_view_counts.values():
        if count > max_count:
            max_count = count
    return max_count

# Helper: Normalize personas scores (assumes a list of numbers).
def normalize_personas(cool_personas):
    if not cool_personas:
        return []
    max_score = max(cool_personas)
    min_score = min(cool_personas)
    normalized = []
    for score in cool_personas:
        if max_score == min_score:
            norm = 0.0
        else:
            norm = (score - min_score) / (max_score - min_score)
        normalized.append(norm)
    return normalized


# Helper: Return key with highest value from a dictionary.
def get_key_with_highest_value(json_data):
    max_key = None
    max_value = -math.inf
    for key, value in json_data.items():
        if value > max_value:
            max_value = value
            max_key = key
    return max_key


# Helper: Generate a moniker string from persona data and time-of-day counts.
def generate_moniker(pers, time_of_day_shares):

    # Composite scores:
    composite_expressiveness = pers["chattiness"]
    composite_watching = (pers["patience"] + pers["binge_level"]) / 2

    adjectives_for_expressiveness = [
        "Quiet", "Reserved", "Mellow", "Chatty", "Talkative", "Expressive", "Vocal", "Outgoing"
    ]
    adjectives_for_enthusiasm = [
        "Cool", "Steady", "Curious", "Cheerful", "Energetic", "Exited", "Spirited", "Ecstatic"
    ]
    adjectives_for_watching = [
        "Nibbling", "Sampling", "Dabbling", "Exploring", "Appreciating", "Enthusiastic", "Savouring", "Connoisseuring"
    ]

    day_person_label = get_key_with_highest_value(time_of_day_shares)

    index_expressiveness = int(math.floor(composite_expressiveness * len(adjectives_for_expressiveness)))
    if index_expressiveness >= len(adjectives_for_expressiveness):
        index_expressiveness = len(adjectives_for_expressiveness) - 1

    index_enthusiasm = int(math.floor(pers["enthusiasm"] * len(adjectives_for_enthusiasm)))
    if index_enthusiasm >= len(adjectives_for_enthusiasm):
        index_enthusiasm = len(adjectives_for_enthusiasm) - 1

    index_watching = int(math.floor(composite_watching * len(adjectives_for_watching)))
    if index_watching >= len(adjectives_for_watching):
        index_watching = len(adjectives_for_watching) - 1

    return f"{adjectives_for_expressiveness[index_expressiveness]}, {adjectives_for_enthusiasm[index_enthusiasm]}, {adjectives_for_watching[index_watching]} {day_person_label}"


# Compute counts of videos by day of week.
# In JavaScript, getDay() returns 0 for Sunday through 6 for Saturday.
def get_weekday_shares(videos):
    counts = [0] * 7
    for video in videos:
        dt = parse_date(video.get("date"))
        # Python's weekday() returns 0 (Monday) through 6 (Sunday).
        # To match JS (Sunday=0), we shift: (weekday + 1) % 7.
        day_index = (dt.weekday() + 1) % 7
        counts[day_index] += 1
    return list(map(lambda x: x/len(videos), counts))


# Compute counts by time-of-day buckets.
def get_time_of_day_shares(videos):
    counts = {"Morning Person": 0, "Afternoon Ace": 0, "Night Owl": 0, "Overnighter": 0}
    for video in videos:
        dt = parse_date(video.get("date"))
        hour = dt.hour
        if 7 <= hour < 12:
            counts["Morning Person"] += 1
        elif 12 <= hour < 18:
            counts["Afternoon Ace"] += 1
        elif 18 <= hour < 24:
            counts["Night Owl"] += 1
        else:
            counts["Overnighter"] += 1
    return {k: v / len(videos) for k, v in counts.items()}


# Compute hourly counts – an array of 24 counts (one per hour).
def get_hourly_shares(videos):
    counts = [0] * 24
    for video in videos:
        dt = parse_date(video.get("date"))
        counts[dt.hour] += 1
    return [c / len(videos) for c in counts ]


# Computes the share of the top N values in the list relative to the total sum.
def share_of_top_n(shares, n):
    if not shares or sum(shares) == 0:
        return 0
    sorted_shares = sorted(shares, reverse=True)
    return sum(sorted_shares[:n])


# Compute counts of videos per week.
def get_weekly_counts(videos):
    weekly_counts = {}
    for video in videos:
        dt = parse_date(video.get("date"))
        key = f"{dt.year}-W{get_week_number(dt)}"
        weekly_counts[key] = weekly_counts.get(key, 0) + 1
    
    # Sort the keys based on year and week number.
    def sort_key(label):
        year_str, week_str = label.split("-W")
        return (int(year_str), int(week_str))
    
    labels = sorted(weekly_counts.keys(), key=sort_key)
    data = [weekly_counts[label] for label in labels]
    return {"labels": labels, "data": data}


def dict_keys_to_lower(d):
    if isinstance(d, dict):
        return {k.lower(): dict_keys_to_lower(v) for k, v in d.items()}
    elif isinstance(d, list):
        return [dict_keys_to_lower(i) for i in d]
    else:
        return d


def calc_median(durations):
    # Compute median viewing time.
    median_viewing_time = 0
    if not durations is None:
        sorted_durations = sorted(durations)
        mid = len(sorted_durations) // 2
        if len(sorted_durations) % 2 == 0:
            median_viewing_time = (sorted_durations[mid - 1] + sorted_durations[mid]) / 2
        else:
            median_viewing_time = sorted_durations[mid]
    return median_viewing_time


# Process the JSON data.
def calc_persona_stats_2(payload, post_woodford=True):

    if post_woodford:
        no_data = []
    else:
        no_data = None

    # Extract videos from possible locations.
    videos = []
    if (payload.get("Your Activity") and
        payload["Your Activity"].get("Watch History") and 
        payload["Your Activity"]["Watch History"].get("VideoList")):
        videos = payload["Your Activity"]["Watch History"]["VideoList"]
    if not videos is None and (payload.get("Activity") and 
                       payload["Activity"].get("Video Browsing History") and 
                       payload["Activity"]["Video Browsing History"].get("VideoList")):
        videos = payload["Activity"]["Video Browsing History"]["VideoList"]

    # Extract posts, comments, and likes.
    posts = (payload.get("Post", {})
                    .get("Posts", {})
                    .get("VideoList", no_data))
    comments = (payload.get("Comment", {})
                       .get("Comments", {})
                       .get("CommentsList", no_data))
    likes = (payload.get("Your Activity", {})
                     .get("Like List", {})
                     .get("ItemFavoriteList", no_data))

    if not post_woodford:
        # If post_woodford is False, set videos, posts, comments, and likes to None.
        posts = None
        comments = None
        likes = None        

    #print(videos[0].keys() if videos else [], posts[0].keys() if posts else [], comments[0].keys() if comments else [], likes[0].keys() if likes else [])
    #print(len(videos), len(posts), len(comments), len(likes))
    if not videos is None:
        videos = dict_keys_to_lower(videos)
        videos = [v for v in videos if isinstance(v, dict) and v.get("date")]
        videos = sorted(videos, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(videos) > 0:
            first_video_date = parse_date(videos[0].get("date"))
            last_video_date = parse_date(videos[-1].get("date"))
            video_days = max((last_video_date - first_video_date).days, 1)
        else:
            video_days = 1
        video_count = len(videos)
        videos_per_day = video_count / video_days
    else:
        video_days = None
        video_count = None
        videos_per_day = None


    if not posts is None:
        posts = dict_keys_to_lower(posts)
        posts = [item for item in posts if not item is None]
        posts = [p for p in posts if isinstance(p, dict) and p.get("date")]
        posts = sorted(posts, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(posts) > 0:
            first_post_date = parse_date(posts[0].get("date"))
            last_post_date = parse_date(posts[-1].get("date"))
            post_days = max((last_post_date - first_post_date).days, 1)
        else:
            post_days = 1
        posts_per_day = len(posts) / post_days
    else:
        post_days = None
        posts_per_day = None


    if not comments is None:
        comments = dict_keys_to_lower(comments)
        comments = [item for item in comments if not item is None]
        comments = [c for c in comments if isinstance(c, dict) and c.get("date")]
        comments = sorted(comments, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(comments) > 0:
            first_comment_date = parse_date(comments[0].get("date"))
            last_comment_date = parse_date(comments[-1].get("date"))
            comment_days = max((last_comment_date - first_comment_date).days, 1)
        else:
            comment_days = 1
        comments_per_day = len(comments) / comment_days
    else:
        comment_days = None
        comments_per_day = None


    if not likes is None:
        likes = [l for l in likes if isinstance(l, dict) and l.get("date")]
        likes = sorted(likes, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        likes = dict_keys_to_lower(likes)
        if len(likes) > 0:
            first_like_date = parse_date(likes[0].get("date"))
            last_like_date = parse_date(likes[-1].get("date"))
            like_days = max((last_like_date - first_like_date).days, 1)
        else:
            like_days = 1
        likes_per_day = len(likes) / like_days
    else:
        like_days = None
        likes_per_day = None


        
    # Compute viewing durations (difference between consecutive video timestamps in seconds)
    durations = []
    session_count = 0
    longest_session = 0.0
    if not videos is None and len(videos) > 0:
        for i in range(1, len(videos)):
            delta = parse_date(videos[i].get("date")) - parse_date(videos[i-1].get("date"))
            durations.append(delta.total_seconds())
    
        # Compute sessions: group consecutive durations <= 300 seconds.
        sessions = [0.0]
        for duration in durations:
            if duration <= 300:
                sessions[session_count] += duration
                if sessions[session_count] > longest_session:
                    longest_session = sessions[session_count]
            elif sessions[session_count] > 0:
                session_count += 1
                sessions.append(0.0)
        
        sessions_per_day = session_count / video_days
        sessions_longer_than_20m = sum(1 for s in sessions if s >= 20 * 60)
    else:
        sessions_per_day = 0
        sessions_longer_than_20m = 0

    # Only keep durations that do not exceed 300 seconds.
    durations = [d for d in durations if d <= 300]

    # calculate some stats on durations
    total_viewing_time = sum(durations)
    avg_viewing_time = total_viewing_time / max(1,len(durations))
    median_viewing_time = calc_median(durations)

    count_less_3   = sum(1 for d in durations if d < 3)
    count_3to6     = sum(1 for d in durations if 3 <= d < 6)
    count_6to15    = sum(1 for d in durations if 6 <= d < 15)
    count_15to30   = sum(1 for d in durations if 15 <= d < 30)
    count_30to60   = sum(1 for d in durations if 30 <= d < 60)
    count_more_60  = sum(1 for d in durations if d >= 60)

    durations_data = {
        "perc_less_3": count_less_3/video_count,
        "perc_3to6": count_3to6/video_count,
        "perc_6to15": count_6to15/video_count,
        "perc_15to30": count_15to30/video_count,
        "perc_30to60": count_30to60/video_count,
        "perc_more_60": count_more_60/video_count,
    }


    # Compute time-of-day distribution.
    if not videos is None and len(videos) > 0:
        hourly_shares = get_hourly_shares(videos)


    if not posts is None:
        total_post_likes = sum_post_likes(posts)
    else:
        total_post_likes = None


    video_view_counts = aggregate_video_views(videos)
    sorted_view_counts = sorted(video_view_counts.values(), reverse=True)
    perc_of_views_top_10pc_videos = sum(sorted_view_counts[:math.floor(len(sorted_view_counts) * 0.1)]) / sum(sorted_view_counts)
    perc_of_views_top_video = sorted_view_counts[0] / sum(sorted_view_counts)
    most_viewed_video = get_most_viewed_video_link(video_view_counts)
    most_viewed_video_count = get_most_viewed_video_counts(video_view_counts)




    # Compute persona data.

    consistency = share_of_top_n(hourly_shares, 2)

    patience = durations_data["perc_30to60"] + durations_data["perc_more_60"]

    if not comments is None:
        chattiness = comments_per_day / videos_per_day
        chattiness = min(1,chattiness)
    else:
        chattiness = None

    if not likes is None:
        enthusiasm = likes_per_day / videos_per_day
        enthusiasm = min(1,enthusiasm)
    else:
        enthusiasm = None

    binge_level = sessions_longer_than_20m / session_count if session_count > 0 else 0

    if not comments is None:
        most_freq_emoji, emoji_count, comment_char_count = get_most_frequent_emoji(comments)
        emoji_level = emoji_count / comment_char_count if ((not comment_char_count is None) and (comment_char_count > 0)) else 0
        emoji_level = math.log(1 + emoji_level)
    else:
        emoji_level = None
        most_freq_emoji = None

    
    result = {
        "longest_session_(s)": longest_session,
        "most_freq_emoji": most_freq_emoji,
        "viewing_time_per_day_(s)": total_viewing_time / video_days,
        'median_viewing_time_per_video_(s)': median_viewing_time,
        'avg_viewing_time_per_video_(s)': avg_viewing_time,
        'perc_of_views_top_10pc_videos': perc_of_views_top_10pc_videos,
        'perc_of_views_top_video': perc_of_views_top_video,
        "time_of_day_shares": get_time_of_day_shares(videos),
        "chattiness": chattiness,
        "patience": patience,
        "enthusiasm": enthusiasm,
        "consistency": consistency,
        "binge_level": binge_level,
        "emoji_level": emoji_level,
        "hourly_shares": hourly_shares,
        "weekday_shares": get_weekday_shares(videos),
        'videos_per_day': videos_per_day,
        'sessions_per_day': sessions_per_day,
        'posts_per_day': posts_per_day,
        'comments_per_day': comments_per_day,
        'likes_per_day': likes_per_day,
        "post_woodford": post_woodford*1
    }


    return result




# Process the JSON data.
def calc_durations(payload):


    # Extract videos from possible locations.
    videos = []
    if (payload.get("Your Activity") and
        payload["Your Activity"].get("Watch History") and 
        payload["Your Activity"]["Watch History"].get("VideoList")):
        videos = payload["Your Activity"]["Watch History"]["VideoList"]
    if not videos is None and (payload.get("Activity") and 
                       payload["Activity"].get("Video Browsing History") and 
                       payload["Activity"]["Video Browsing History"].get("VideoList")):
        videos = payload["Activity"]["Video Browsing History"]["VideoList"]


    if not videos is None:
        videos = dict_keys_to_lower(videos)
        videos = [v for v in videos if isinstance(v, dict) and v.get("date")]
        videos = sorted(videos, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        
    # Compute viewing durations (difference between consecutive video timestamps in seconds)
    durations = []

    if not videos is None and len(videos) > 0:
        for i in range(1, len(videos)):
            delta = parse_date(videos[i].get("date")) - parse_date(videos[i-1].get("date"))
            durations.append(delta.total_seconds())
        
        durations.append(None)


    # replace durations longer than 300 seconds with None.
    durations = [d if (not d is None) and (d <= 300) else None for d in durations]


    return len(durations)



# Process the JSON data.
def calc_persona_stats_2(payload, post_woodford=True):

    if post_woodford:
        no_data = []
    else:
        no_data = None

    # Extract videos from possible locations.
    videos = []
    if (payload.get("Your Activity") and
        payload["Your Activity"].get("Watch History") and 
        payload["Your Activity"]["Watch History"].get("VideoList")):
        videos = payload["Your Activity"]["Watch History"]["VideoList"]
    if not videos is None and (payload.get("Activity") and 
                       payload["Activity"].get("Video Browsing History") and 
                       payload["Activity"]["Video Browsing History"].get("VideoList")):
        videos = payload["Activity"]["Video Browsing History"]["VideoList"]

    # Extract posts, comments, and likes.
    posts = (payload.get("Post", {})
                    .get("Posts", {})
                    .get("VideoList", no_data))
    comments = (payload.get("Comment", {})
                       .get("Comments", {})
                       .get("CommentsList", no_data))
    likes = (payload.get("Your Activity", {})
                     .get("Like List", {})
                     .get("ItemFavoriteList", no_data))

    if not post_woodford:
        # If post_woodford is False, set videos, posts, comments, and likes to None.
        posts = None
        comments = None
        likes = None        

    #print(videos[0].keys() if videos else [], posts[0].keys() if posts else [], comments[0].keys() if comments else [], likes[0].keys() if likes else [])
    #print(len(videos), len(posts), len(comments), len(likes))
    if not videos is None:
        videos = dict_keys_to_lower(videos)
        videos = [v for v in videos if isinstance(v, dict) and v.get("date")]
        videos = sorted(videos, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(videos) > 0:
            first_video_date = parse_date(videos[0].get("date"))
            last_video_date = parse_date(videos[-1].get("date"))
            video_days = max((last_video_date - first_video_date).days, 1)
        else:
            video_days = 1
        video_count = len(videos)
        videos_per_day = video_count / video_days
    else:
        video_days = None
        video_count = None
        videos_per_day = None


    if not posts is None:
        posts = dict_keys_to_lower(posts)
        posts = [item for item in posts if not item is None]
        posts = [p for p in posts if isinstance(p, dict) and p.get("date")]
        posts = sorted(posts, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(posts) > 0:
            first_post_date = parse_date(posts[0].get("date"))
            last_post_date = parse_date(posts[-1].get("date"))
            post_days = max((last_post_date - first_post_date).days, 1)
        else:
            post_days = 1
        posts_per_day = len(posts) / post_days
    else:
        post_days = None
        posts_per_day = None


    if not comments is None:
        comments = dict_keys_to_lower(comments)
        comments = [item for item in comments if not item is None]
        comments = [c for c in comments if isinstance(c, dict) and c.get("date")]
        comments = sorted(comments, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(comments) > 0:
            first_comment_date = parse_date(comments[0].get("date"))
            last_comment_date = parse_date(comments[-1].get("date"))
            comment_days = max((last_comment_date - first_comment_date).days, 1)
        else:
            comment_days = 1
        comments_per_day = len(comments) / comment_days
    else:
        comment_days = None
        comments_per_day = None


    if not likes is None:
        likes = [l for l in likes if isinstance(l, dict) and l.get("date")]
        likes = sorted(likes, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        likes = dict_keys_to_lower(likes)
        if len(likes) > 0:
            first_like_date = parse_date(likes[0].get("date"))
            last_like_date = parse_date(likes[-1].get("date"))
            like_days = max((last_like_date - first_like_date).days, 1)
        else:
            like_days = 1
        likes_per_day = len(likes) / like_days
    else:
        like_days = None
        likes_per_day = None


        
    # Compute viewing durations (difference between consecutive video timestamps in seconds)
    durations = []
    session_count = 0
    longest_session = 0.0
    if not videos is None and len(videos) > 0:
        for i in range(1, len(videos)):
            delta = parse_date(videos[i].get("date")) - parse_date(videos[i-1].get("date"))
            durations.append(delta.total_seconds())
    
        # Compute sessions: group consecutive durations <= 300 seconds.
        sessions = [0.0]
        for duration in durations:
            if duration <= 300:
                sessions[session_count] += duration
                if sessions[session_count] > longest_session:
                    longest_session = sessions[session_count]
            elif sessions[session_count] > 0:
                session_count += 1
                sessions.append(0.0)
        
        sessions_per_day = session_count / video_days
        sessions_longer_than_20m = sum(1 for s in sessions if s >= 20 * 60)
    else:
        sessions_per_day = 0
        sessions_longer_than_20m = 0

    # Only keep durations that do not exceed 300 seconds.
    durations = [d for d in durations if d <= 300]

    # calculate some stats on durations
    total_viewing_time = sum(durations)
    avg_viewing_time = total_viewing_time / max(1,len(durations))
    median_viewing_time = calc_median(durations)

    count_less_3   = sum(1 for d in durations if d < 3)
    count_3to6     = sum(1 for d in durations if 3 <= d < 6)
    count_6to15    = sum(1 for d in durations if 6 <= d < 15)
    count_15to30   = sum(1 for d in durations if 15 <= d < 30)
    count_30to60   = sum(1 for d in durations if 30 <= d < 60)
    count_more_60  = sum(1 for d in durations if d >= 60)

    durations_data = {
        "perc_less_3": count_less_3/video_count,
        "perc_3to6": count_3to6/video_count,
        "perc_6to15": count_6to15/video_count,
        "perc_15to30": count_15to30/video_count,
        "perc_30to60": count_30to60/video_count,
        "perc_more_60": count_more_60/video_count,
    }


    # Compute time-of-day distribution.
    if not videos is None and len(videos) > 0:
        hourly_shares = get_hourly_shares(videos)


    if not posts is None:
        total_post_likes = sum_post_likes(posts)
    else:
        total_post_likes = None


    video_view_counts = aggregate_video_views(videos)
    sorted_view_counts = sorted(video_view_counts.values(), reverse=True)
    perc_of_views_top_10pc_videos = sum(sorted_view_counts[:math.floor(len(sorted_view_counts) * 0.1)]) / sum(sorted_view_counts)
    perc_of_views_top_video = sorted_view_counts[0] / sum(sorted_view_counts)
    most_viewed_video = get_most_viewed_video_link(video_view_counts)
    most_viewed_video_count = get_most_viewed_video_counts(video_view_counts)




    # Compute persona data.

    consistency = share_of_top_n(hourly_shares, 2)

    patience = durations_data["perc_30to60"] + durations_data["perc_more_60"]

    if not comments is None:
        chattiness = comments_per_day / videos_per_day
        chattiness = min(1,chattiness)
    else:
        chattiness = None

    if not likes is None:
        enthusiasm = likes_per_day / videos_per_day
        enthusiasm = min(1,enthusiasm)
    else:
        enthusiasm = None

    binge_level = sessions_longer_than_20m / session_count if session_count > 0 else 0

    if not comments is None:
        most_freq_emoji, emoji_count, comment_char_count = get_most_frequent_emoji(comments)
        emoji_level = emoji_count / comment_char_count if ((not comment_char_count is None) and (comment_char_count > 0)) else 0
        emoji_level = math.log(1 + emoji_level)
    else:
        emoji_level = None
        most_freq_emoji = None

    
    result = {
        "longest_session_(s)": longest_session,
        "most_freq_emoji": most_freq_emoji,
        "viewing_time_per_day_(s)": total_viewing_time / video_days,
        'median_viewing_time_per_video_(s)': median_viewing_time,
        'avg_viewing_time_per_video_(s)': avg_viewing_time,
        'perc_of_views_top_10pc_videos': perc_of_views_top_10pc_videos,
        'perc_of_views_top_video': perc_of_views_top_video,
        "time_of_day_shares": get_time_of_day_shares(videos),
        "chattiness": chattiness,
        "patience": patience,
        "enthusiasm": enthusiasm,
        "consistency": consistency,
        "binge_level": binge_level,
        "emoji_level": emoji_level,
        "hourly_shares": hourly_shares,
        "weekday_shares": get_weekday_shares(videos),
        'videos_per_day': videos_per_day,
        'sessions_per_day': sessions_per_day,
        'posts_per_day': posts_per_day,
        'comments_per_day': comments_per_day,
        'likes_per_day': likes_per_day,
        "post_woodford": post_woodford*1
    }


    return result




# Process the JSON data.
def calc_durations(payload):


    # Extract videos from possible locations.
    videos = []
    if (payload.get("Your Activity") and
        payload["Your Activity"].get("Watch History") and 
        payload["Your Activity"]["Watch History"].get("VideoList")):
        videos = payload["Your Activity"]["Watch History"]["VideoList"]
    if not videos is None and (payload.get("Activity") and 
                       payload["Activity"].get("Video Browsing History") and 
                       payload["Activity"]["Video Browsing History"].get("VideoList")):
        videos = payload["Activity"]["Video Browsing History"]["VideoList"]


    if not videos is None:
        videos = dict_keys_to_lower(videos)
        videos = [v for v in videos if isinstance(v, dict) and v.get("date")]
        videos = sorted(videos, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        
    # Compute viewing durations (difference between consecutive video timestamps in seconds)
    durations = []

    if not videos is None and len(videos) > 0:
        for i in range(1, len(videos)):
            delta = parse_date(videos[i].get("date")) - parse_date(videos[i-1].get("date"))
            durations.append(delta.total_seconds())
        
        durations.append(None)


    # replace durations longer than 300 seconds with None.
    durations = [d if (not d is None) and (d <= 300) else None for d in durations]


    return len(durations)



# Process the JSON data.
def calc_persona_stats_2(payload, post_woodford=True):

    if post_woodford:
        no_data = []
    else:
        no_data = None

    # Extract videos from possible locations.
    videos = []
    if (payload.get("Your Activity") and
        payload["Your Activity"].get("Watch History") and 
        payload["Your Activity"]["Watch History"].get("VideoList")):
        videos = payload["Your Activity"]["Watch History"]["VideoList"]
    if not videos is None and (payload.get("Activity") and 
                       payload["Activity"].get("Video Browsing History") and 
                       payload["Activity"]["Video Browsing History"].get("VideoList")):
        videos = payload["Activity"]["Video Browsing History"]["VideoList"]

    # Extract posts, comments, and likes.
    posts = (payload.get("Post", {})
                    .get("Posts", {})
                    .get("VideoList", no_data))
    comments = (payload.get("Comment", {})
                       .get("Comments", {})
                       .get("CommentsList", no_data))
    likes = (payload.get("Your Activity", {})
                     .get("Like List", {})
                     .get("ItemFavoriteList", no_data))

    if not post_woodford:
        # If post_woodford is False, set videos, posts, comments, and likes to None.
        posts = None
        comments = None
        likes = None        

    #print(videos[0].keys() if videos else [], posts[0].keys() if posts else [], comments[0].keys() if comments else [], likes[0].keys() if likes else [])
    #print(len(videos), len(posts), len(comments), len(likes))
    if not videos is None:
        videos = dict_keys_to_lower(videos)
        videos = [v for v in videos if isinstance(v, dict) and v.get("date")]
        videos = sorted(videos, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(videos) > 0:
            first_video_date = parse_date(videos[0].get("date"))
            last_video_date = parse_date(videos[-1].get("date"))
            video_days = max((last_video_date - first_video_date).days, 1)
        else:
            video_days = 1
        video_count = len(videos)
        videos_per_day = video_count / video_days
    else:
        video_days = None
        video_count = None
        videos_per_day = None


    if not posts is None:
        posts = dict_keys_to_lower(posts)
        posts = [item for item in posts if not item is None]
        posts = [p for p in posts if isinstance(p, dict) and p.get("date")]
        posts = sorted(posts, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(posts) > 0:
            first_post_date = parse_date(posts[0].get("date"))
            last_post_date = parse_date(posts[-1].get("date"))
            post_days = max((last_post_date - first_post_date).days, 1)
        else:
            post_days = 1
        posts_per_day = len(posts) / post_days
    else:
        post_days = None
        posts_per_day = None


    if not comments is None:
        comments = dict_keys_to_lower(comments)
        comments = [item for item in comments if not item is None]
        comments = [c for c in comments if isinstance(c, dict) and c.get("date")]
        comments = sorted(comments, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        if len(comments) > 0:
            first_comment_date = parse_date(comments[0].get("date"))
            last_comment_date = parse_date(comments[-1].get("date"))
            comment_days = max((last_comment_date - first_comment_date).days, 1)
        else:
            comment_days = 1
        comments_per_day = len(comments) / comment_days
    else:
        comment_days = None
        comments_per_day = None


    if not likes is None:
        likes = [l for l in likes if isinstance(l, dict) and l.get("date")]
        likes = sorted(likes, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        likes = dict_keys_to_lower(likes)
        if len(likes) > 0:
            first_like_date = parse_date(likes[0].get("date"))
            last_like_date = parse_date(likes[-1].get("date"))
            like_days = max((last_like_date - first_like_date).days, 1)
        else:
            like_days = 1
        likes_per_day = len(likes) / like_days
    else:
        like_days = None
        likes_per_day = None


        
    # Compute viewing durations (difference between consecutive video timestamps in seconds)
    durations = []
    session_count = 0
    longest_session = 0.0
    if not videos is None and len(videos) > 0:
        for i in range(1, len(videos)):
            delta = parse_date(videos[i].get("date")) - parse_date(videos[i-1].get("date"))
            durations.append(delta.total_seconds())
    
        # Compute sessions: group consecutive durations <= 300 seconds.
        sessions = [0.0]
        for duration in durations:
            if duration <= 300:
                sessions[session_count] += duration
                if sessions[session_count] > longest_session:
                    longest_session = sessions[session_count]
            elif sessions[session_count] > 0:
                session_count += 1
                sessions.append(0.0)
        
        sessions_per_day = session_count / video_days
        sessions_longer_than_20m = sum(1 for s in sessions if s >= 20 * 60)
    else:
        sessions_per_day = 0
        sessions_longer_than_20m = 0

    # Only keep durations that do not exceed 300 seconds.
    durations = [d for d in durations if d <= 300]

    # calculate some stats on durations
    total_viewing_time = sum(durations)
    avg_viewing_time = total_viewing_time / max(1,len(durations))
    median_viewing_time = calc_median(durations)

    count_less_3   = sum(1 for d in durations if d < 3)
    count_3to6     = sum(1 for d in durations if 3 <= d < 6)
    count_6to15    = sum(1 for d in durations if 6 <= d < 15)
    count_15to30   = sum(1 for d in durations if 15 <= d < 30)
    count_30to60   = sum(1 for d in durations if 30 <= d < 60)
    count_more_60  = sum(1 for d in durations if d >= 60)

    durations_data = {
        "perc_less_3": count_less_3/video_count,
        "perc_3to6": count_3to6/video_count,
        "perc_6to15": count_6to15/video_count,
        "perc_15to30": count_15to30/video_count,
        "perc_30to60": count_30to60/video_count,
        "perc_more_60": count_more_60/video_count,
    }


    # Compute time-of-day distribution.
    if not videos is None and len(videos) > 0:
        hourly_shares = get_hourly_shares(videos)


    if not posts is None:
        total_post_likes = sum_post_likes(posts)
    else:
        total_post_likes = None


    video_view_counts = aggregate_video_views(videos)
    sorted_view_counts = sorted(video_view_counts.values(), reverse=True)
    perc_of_views_top_10pc_videos = sum(sorted_view_counts[:math.floor(len(sorted_view_counts) * 0.1)]) / sum(sorted_view_counts)
    perc_of_views_top_video = sorted_view_counts[0] / sum(sorted_view_counts)
    most_viewed_video = get_most_viewed_video_link(video_view_counts)
    most_viewed_video_count = get_most_viewed_video_counts(video_view_counts)




    # Compute persona data.

    consistency = share_of_top_n(hourly_shares, 2)

    patience = durations_data["perc_30to60"] + durations_data["perc_more_60"]

    if not comments is None:
        chattiness = comments_per_day / videos_per_day
        chattiness = min(1,chattiness)
    else:
        chattiness = None

    if not likes is None:
        enthusiasm = likes_per_day / videos_per_day
        enthusiasm = min(1,enthusiasm)
    else:
        enthusiasm = None

    binge_level = sessions_longer_than_20m / session_count if session_count > 0 else 0

    if not comments is None:
        most_freq_emoji, emoji_count, comment_char_count = get_most_frequent_emoji(comments)
        emoji_level = emoji_count / comment_char_count if ((not comment_char_count is None) and (comment_char_count > 0)) else 0
        emoji_level = math.log(1 + emoji_level)
    else:
        emoji_level = None
        most_freq_emoji = None

    
    result = {
        "longest_session_(s)": longest_session,
        "most_freq_emoji": most_freq_emoji,
        "viewing_time_per_day_(s)": total_viewing_time / video_days,
        'median_viewing_time_per_video_(s)': median_viewing_time,
        'avg_viewing_time_per_video_(s)': avg_viewing_time,
        'perc_of_views_top_10pc_videos': perc_of_views_top_10pc_videos,
        'perc_of_views_top_video': perc_of_views_top_video,
        "time_of_day_shares": get_time_of_day_shares(videos),
        "chattiness": chattiness,
        "patience": patience,
        "enthusiasm": enthusiasm,
        "consistency": consistency,
        "binge_level": binge_level,
        "emoji_level": emoji_level,
        "hourly_shares": hourly_shares,
        "weekday_shares": get_weekday_shares(videos),
        'videos_per_day': videos_per_day,
        'sessions_per_day': sessions_per_day,
        'posts_per_day': posts_per_day,
        'comments_per_day': comments_per_day,
        'likes_per_day': likes_per_day,
        "post_woodford": post_woodford*1
    }


    return result




# Process the JSON data.
def calc_durations(payload):


    # Extract videos from possible locations.
    videos = []
    if (payload.get("Your Activity") and
        payload["Your Activity"].get("Watch History") and 
        payload["Your Activity"]["Watch History"].get("VideoList")):
        videos = payload["Your Activity"]["Watch History"]["VideoList"]
    if not videos is None and (payload.get("Activity") and 
                       payload["Activity"].get("Video Browsing History") and 
                       payload["Activity"]["Video Browsing History"].get("VideoList")):
        videos = payload["Activity"]["Video Browsing History"]["VideoList"]


    if not videos is None:
        videos = dict_keys_to_lower(videos)
        videos = [v for v in videos if isinstance(v, dict) and v.get("date")]
        videos = sorted(videos, key=lambda item: parse_date(item.get("date", "1970-01-01 00:00:00")))
        
    # Compute viewing durations (difference between consecutive video timestamps in seconds)
    durations = []

    if not videos is None and len(videos) > 0:
        for i in range(1, len(videos)):
            delta = parse_date(videos[i].get("date")) - parse_date(videos[i-1].get("date"))
            durations.append(delta.total_seconds())
        
        durations.append(None)


    # replace durations longer than 300 seconds with None.
    durations = [d if (not d is None) and (d <= 300) else None for d in durations]


    return len(durations)



