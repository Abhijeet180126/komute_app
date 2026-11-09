"""Route Demand: riders searching near a driver's posted rides (API
module 3). Moved verbatim from komute/views.py route_demand_view, which
renders what build() returns; the API serializes it."""
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from komute.hashid_utils import encode_id
from komute.models import RideInvite, Rides, RideSearch, UserBlock
from komute.services.common import display_name
from komute.services.geo import geo_box_q, haversine_km
from komute.services.rides import gender_pref_allows


def build(driver):
    """{'route_demands': [...], 'stats': {...}, 'has_active_rides': bool} for
    a driver. Riders' 'id' is their database id (the page posts it back to
    send an invite; the API hands out hashids instead)."""
    from collections import Counter
    from django.utils.timesince import timesince as _timesince

    now      = timezone.now()
    today    = now.date()
    week_ago = now - timedelta(days=7)

    # Start from the driver's posted rides — always the anchor
    driver_rides = list(
        Rides.objects
        .filter(driver=driver, status__in=['posted', 'full'])
        .prefetch_related('bookings')
        .order_by('depart_at')
    )

    # Searches by logged-in users in the past 7 days, narrowed in SQL to the
    # boxes around the driver's routes (audit F-3). Recurring rides share a
    # route, so each distinct route adds one box pair.
    radius = settings.SEARCH_RADIUS_KM
    routes = {(r.origin_lat, r.origin_lng, r.dest_lat, r.dest_lng) for r in driver_rides}
    near_routes = Q(pk__in=[])
    for o_lat, o_lng, d_lat, d_lng in routes:
        near_routes |= (geo_box_q(o_lat, o_lng, radius, 'origin_lat', 'origin_lng')
                        & geo_box_q(d_lat, d_lng, radius, 'dest_lat', 'dest_lng'))
    recent_searches = list(
        RideSearch.objects
        .filter(near_routes, created_at__gte=week_ago, user__isnull=False, user__is_active=True)
        .exclude(user=driver)
        # Blocked riders can't be invited, so don't list them (ISSUES.md A1).
        .exclude(user_id__in=UserBlock.objects.hidden_ids(driver.id))
        .select_related('user')
        .order_by('-created_at')
    ) if routes else []

    # Riders who searched within the radius of a route's origin AND
    # destination, computed once per route (newest first).
    route_matches = {
        key: [
            s for s in recent_searches
            if haversine_km(key[0], key[1], s.origin_lat, s.origin_lng) <= radius
            and haversine_km(key[2], key[3], s.dest_lat, s.dest_lng) <= radius
        ]
        for key in routes
    }

    AVATAR_COLORS = [
        'linear-gradient(135deg,#7c3aed,#a78bfa)',
        'linear-gradient(135deg,#0C63B4,#38bdf8)',
        'linear-gradient(135deg,#dc2626,#f87171)',
        'linear-gradient(135deg,#047857,#34d399)',
        'linear-gradient(135deg,#d97706,#fbbf24)',
        'linear-gradient(135deg,#be185d,#f472b6)',
        'linear-gradient(135deg,#0C63B4,#0CB439)',
        'linear-gradient(135deg,#0e7490,#22d3ee)',
    ]

    route_demands = []
    for ride in driver_rides:
        matching_searches = route_matches[
            (ride.origin_lat, ride.origin_lng, ride.dest_lat, ride.dest_lng)]

        count  = len(matching_searches)
        is_new = any(s.created_at.date() == today for s in matching_searches)

        if count >= 6:
            heat, demand_label = 'hot',  'High demand 🔥'
            demand_percent = min(100, count * 8)
        elif count >= 4:
            heat, demand_label = 'warm', 'Moderate demand'
            demand_percent = min(100, count * 10)
        elif count > 0:
            heat, demand_label = 'cool', 'Low demand'
            demand_percent = min(100, count * 12)
        else:
            heat, demand_label = 'cool', 'No searches yet'
            demand_percent = 0

        if matching_searches:
            weekdays = Counter(s.search_date.strftime('%a') for s in matching_searches)
            top_days = [d for d, _ in weekdays.most_common(2)]
            most_searched_days = '–'.join(top_days) if top_days else None
            avg_raw    = sum(s.passenger_count for s in matching_searches) / count
            avg_seats_n = round(avg_raw)
            avg_seats  = (
                f'{avg_raw:.0f}–{avg_seats_n + 1} seats'
                if avg_raw != avg_seats_n
                else f'{avg_seats_n} seat{"s" if avg_seats_n != 1 else ""}'
            )
            latest_search = matching_searches[0]
        else:
            most_searched_days = None
            avg_seats          = None
            latest_search      = None

        # Build per-rider rows (one per unique user). Searches are newest
        # first, so a user's first one here is their latest.
        seen_users = set()
        riders = []
        for s in matching_searches:
            uid = s.user_id
            if uid in seen_users:
                continue
            seen_users.add(uid)
            u    = s.user
            # Not invitable under the ride's gender preference (audit L-8).
            if not gender_pref_allows(ride, u):
                continue
            name = display_name(u)
            initials = ''.join(w[0].upper() for w in name.split()[:2])
            avatar_color = AVATAR_COLORS[uid % len(AVATAR_COLORS)]
            # avg_rating is 0 iff the rider has never received a review --
            # real ratings are 1-5 (Review.rating's MinValueValidator), so a
            # real average can never land on 0.
            rating_label = f'⭐ {round(u.avg_rating, 1)} rider rating' if u.avg_rating else 'Not yet rated'
            riders.append({
                'id':              uid,
                'initials':        initials,
                'avatar_color':    avatar_color,
                'name':            name,
                'searching_since': _timesince(s.created_at),
                'rating_label':    rating_label,
                'seats_needed':    s.passenger_count,
            })

        booked_seats = ride.seats_total - ride.seats_available
        open_seats   = ride.seats_available
        ride_date    = None
        if ride.depart_at.date() != today:
            _d = ride.depart_at
            ride_date = f"{_d.strftime('%b')} {_d.day}"

        route_demands.append({
            'heat':              heat,
            'is_my_route':       True,
            'is_new':            is_new,
            'has_my_ride':       True,
            'ride_id':           encode_id(ride.id),
            'from_location':     ride.origin,
            'to_location':       ride.destination,
            'search_count':      count,
            'demand_label':      demand_label,
            'demand_percent':    demand_percent,
            'match_label':       'Your posted ride',
            'most_searched_days': most_searched_days,
            'specific_date':     None,
            'peak_time':         None,
            'departure_range':   None,
            'avg_seats':         avg_seats,
            'last_updated':      _timesince(latest_search.created_at) if latest_search else None,
            'available_seats':   open_seats,
            'ride_date':         ride_date,
            'booked_seats':      booked_seats,
            'open_seats':        open_seats,
            'riders':            riders,
        })

    # Most-demand rides first
    route_demands.sort(key=lambda d: d['search_count'], reverse=True)

    hot_count       = sum(1 for d in route_demands if d['heat'] == 'hot')
    new_today_count = sum(1 for d in route_demands if d['is_new'])

    stats = {
        'riders_searching':   RideSearch.objects.filter(
                                created_at__gte=week_ago, user__isnull=False
                              ).values('user').distinct().count(),
        'hot_routes':         hot_count,
        'invites_sent_today': RideInvite.objects.filter(
                                driver=driver, sent_at__date=today
                              ).count(),
        'invites_accepted':   RideInvite.objects.filter(
                                driver=driver, status=RideInvite.STATUS_ACCEPTED
                              ).count(),
        'total_count':        len(route_demands),
        'hot_count':          hot_count,
        'my_routes_count':    len(route_demands),
        'new_today_count':    new_today_count,
    }

    has_active_rides = len(driver_rides) > 0

    return {
        'route_demands':   route_demands,
        'stats':           stats,
        'has_active_rides': has_active_rides,
    }
