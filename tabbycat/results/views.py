import json
import datetime
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.template import Context, Template
from django.shortcuts import get_object_or_404, render
from django.views.generic.base import TemplateView
from django.views.decorators.cache import cache_page

from actionlog.models import ActionLogEntry
from adjallocation.allocation import populate_allocations
from adjallocation.models import DebateAdjudicator
from draw.models import Debate, DebateTeam
from draw.prefetch import populate_opponents
from participants.models import Adjudicator
from tournaments.mixins import PublicTournamentPageMixin, RoundMixin
from tournaments.models import Round
from utils.views import public_optional_tournament_view, round_view, tournament_view
from utils.misc import get_ip_address, redirect_round
from utils.mixins import VueTableTemplateView
from utils.tables import TabbycatTableBuilder
from venues.models import Venue

from .result import BallotSet
from .forms import BallotSetForm
from .models import BallotSubmission, TeamScore
from .tables import ResultsTableBuilder
from .prefetch import populate_confirmed_ballots
from .utils import get_result_status_stats

logger = logging.getLogger(__name__)


@login_required
@tournament_view
def toggle_postponed(request, t, debate_id):
    debate = Debate.objects.get(pk=debate_id)
    if debate.result_status == debate.STATUS_POSTPONED:
        debate.result_status = debate.STATUS_NONE
    else:
        debate.result_status = debate.STATUS_POSTPONED

    debate.save()
    return redirect_round('results', debate.round)


class ResultsEntryForRoundView(RoundMixin, LoginRequiredMixin, VueTableTemplateView):

    template_name = 'results.html'

    def _get_draw(self):
        if not hasattr(self, '_draw'):
            if self.request.user.is_superuser:
                filter_kwargs = None
            else:
                filter_kwargs = dict(result_status__in=[Debate.STATUS_NONE, Debate.STATUS_DRAFT])
            self._draw = self.get_round().debate_set_with_prefetches(
                    ordering=('room_rank',), ballotsets=True, wins=True,
                    filter_kwargs=filter_kwargs)
        return self._draw

    def get_table(self):
        draw = self._get_draw()
        table = ResultsTableBuilder(view=self,
            admin=self.request.user.is_superuser, sort_key="Status")
        table.add_ballot_status_columns(draw)
        table.add_ballot_entry_columns(draw)
        table.add_debate_venue_columns(draw)
        table.add_debate_results_columns(draw)
        table.add_debate_adjudicators_column(draw, show_splits=True)
        return table

    def get_context_data(self, **kwargs):
        round = self.get_round()
        result_status_stats = get_result_status_stats(round)

        kwargs["stats"] = {
            'none': result_status_stats[Debate.STATUS_NONE],
            'ballot_in': result_status_stats['B'],
            'draft': result_status_stats[Debate.STATUS_DRAFT],
            'confirmed': result_status_stats[Debate.STATUS_CONFIRMED],
            'postponed': result_status_stats[Debate.STATUS_POSTPONED],
        }

        kwargs["has_motions"] = round.motion_set.count() > 0
        return super().get_context_data(**kwargs)


class PublicResultsForRoundView(RoundMixin, PublicTournamentPageMixin, VueTableTemplateView):

    template_name = "public_results_for_round.html"
    public_page_preference = 'public_results'
    page_title = 'Results'
    page_emoji = '💥'
    default_view = 'team'

    def get_table(self):
        view_type = self.request.session.get('results_view', 'team')
        if view_type == 'debate':
            return self.get_table_by_debate()
        else:
            return self.get_table_by_team()

    def get_table_by_debate(self):
        round = self.get_round()
        tournament = self.get_tournament()
        debates = round.debate_set_with_prefetches(ballotsets=True, wins=True)

        table = TabbycatTableBuilder(view=self, sort_key="Venue")
        table.add_debate_venue_columns(debates)
        table.add_debate_results_columns(debates)
        table.add_debate_ballot_link_column(debates)
        table.add_debate_adjudicators_column(debates, show_splits=True)
        if tournament.pref('show_motions_in_results'):
            table.add_motion_column([d.confirmed_ballot.motion
                if d.confirmed_ballot else None for d in debates])

        return table

    def get_table_by_team(self):
        round = self.get_round()
        tournament = self.get_tournament()
        teamscores = TeamScore.objects.filter(debate_team__debate__round=round,
                ballot_submission__confirmed=True).prefetch_related(
                'debate_team', 'debate_team__team', 'debate_team__team__speaker_set',
                'debate_team__team__institution')
        debates = [ts.debate_team.debate for ts in teamscores]

        populate_opponents([ts.debate_team for ts in teamscores])

        for pos in [DebateTeam.POSITION_AFFIRMATIVE, DebateTeam.POSITION_NEGATIVE]:
            debates_for_pos = [ts.debate_team.debate for ts in teamscores if ts.debate_team.position == pos]
            populate_allocations(debates_for_pos)
            populate_confirmed_ballots(debates_for_pos, motions=True)

        table = TabbycatTableBuilder(view=self, sort_key="Team")
        table.add_team_columns([ts.debate_team.team for ts in teamscores])
        table.add_debate_result_by_team_columns(teamscores)
        table.add_debate_ballot_link_column(debates)
        table.add_debate_adjudicators_column(debates, show_splits=True)
        if tournament.pref('show_motions_in_results'):
            table.add_motion_column([debate.confirmed_ballot.motion
                if debate.confirmed_ballot else None for debate in debates])

        return table

    def get(self, request, *args, **kwargs):
        tournament = self.get_tournament()
        round = self.get_round()
        if round.silent and not tournament.pref('all_results_released'):
            logger.info("Refused results for %s: silent", round.name)
            return render(request, 'public_results_silent.html')
        if round.seq >= tournament.current_round.seq and not tournament.pref('all_results_released'):
            logger.info("Refused results for %s: not yet available", round.name)
            return render(request, 'public_results_not_available.html')

        # If there's a query string, store the session setting
        if request.GET.get('view') in ['team', 'debate']:
            request.session['results_view'] = request.GET['view']

        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        kwargs['view_type'] = self.request.session.get('results_view', self.default_view)
        return super().get_context_data(**kwargs)


class PublicResultsIndexView(PublicTournamentPageMixin, TemplateView):

    template_name = 'public_results_index.html'
    public_page_preference = 'public_results'

    def get_context_data(self, **kwargs):
        tournament = self.get_tournament()
        kwargs["rounds"] = tournament.round_set.filter(
            seq__lt=tournament.current_round.seq,
            silent=False).order_by('seq')
        return super().get_context_data(**kwargs)


@login_required
@tournament_view
def edit_ballotset(request, t, ballotsub_id):
    ballotsub = get_object_or_404(BallotSubmission, id=ballotsub_id)
    debate = ballotsub.debate

    all_ballotsubs = debate.ballotsubmission_set.order_by('version')
    if not request.user.is_superuser:
        all_ballotsubs = all_ballotsubs.exclude(discarded=True)

    identical_ballotsubs_dict = debate.identical_ballotsubs_dict
    for b in all_ballotsubs:
        if b in identical_ballotsubs_dict:
            b.identical_ballotsub_versions = identical_ballotsubs_dict[b]

    if request.method == 'POST':
        form = BallotSetForm(ballotsub, request.POST)

        if form.is_valid():
            form.save()

            if ballotsub.discarded:
                action_type = ActionLogEntry.ACTION_TYPE_BALLOT_DISCARD
                messages.success(request, "Ballot set for %s discarded." % debate.matchup)
            elif ballotsub.confirmed:
                ballotsub.confirmer = request.user
                ballotsub.confirm_timestamp = datetime.datetime.now()
                ballotsub.save()
                action_type = ActionLogEntry.ACTION_TYPE_BALLOT_CONFIRM
                messages.success(request, "Ballot set for %s confirmed." % debate.matchup)
            else:
                action_type = ActionLogEntry.ACTION_TYPE_BALLOT_EDIT
                messages.success(request, "Edits to ballot set for %s saved." % debate.matchup)
            ActionLogEntry.objects.log(type=action_type, user=request.user, ballot_submission=ballotsub,
                                       ip_address=get_ip_address(request), tournament=t)

            return redirect_round('results', debate.round)
    else:
        form = BallotSetForm(ballotsub)

    template = 'enter_results.html' if request.user.is_superuser else 'assistant_enter_results.html'
    context = {
        'form'             : form,
        'ballotsub'        : ballotsub,
        'debate'           : debate,
        'all_ballotsubs'   : all_ballotsubs,
        'disable_confirm'  : request.user == ballotsub.submitter and not t.pref('disable_ballot_confirms') and not request.user.is_superuser,
        'round'            : debate.round,
        'not_singleton'    : all_ballotsubs.exclude(id=ballotsub_id).exists(),
        'new'              : False,
    }
    return render(request, template, context)


# Don't cache
@public_optional_tournament_view('public_ballots_randomised')
def public_new_ballotset_key(request, t, url_key):
    adjudicator = get_object_or_404(Adjudicator, tournament=t, url_key=url_key)
    return public_new_ballotset(request, t, adjudicator)


# Don't cache
@public_optional_tournament_view('public_ballots')
def public_new_ballotset_id(request, t, adj_id):
    adjudicator = get_object_or_404(Adjudicator, tournament=t, id=adj_id)
    return public_new_ballotset(request, t, adjudicator)


def public_new_ballotset(request, t, adjudicator):
    round = t.current_round

    if round.draw_status != Round.STATUS_RELEASED or not round.motions_released:
        return render(request, 'public_enter_results_error.html', dict(
            adjudicator=adjudicator, message='The draw and/or motions for the '
            'round haven\'t been released yet.'))

    try:
        da = DebateAdjudicator.objects.get(adjudicator=adjudicator, debate__round=round)
    except DebateAdjudicator.DoesNotExist:
        return render(request, 'public_enter_results_error.html', dict(
            adjudicator=adjudicator,
            message='It looks like you don\'t have a debate this round.'))

    ip_address = get_ip_address(request)
    ballotsub = BallotSubmission(
        debate=da.debate, ip_address=ip_address,
        submitter_type=BallotSubmission.SUBMITTER_PUBLIC)

    if request.method == 'POST':
        form = BallotSetForm(ballotsub, request.POST, password=True)
        if form.is_valid():
            form.save()
            ActionLogEntry.objects.log(
                type=ActionLogEntry.ACTION_TYPE_BALLOT_SUBMIT,
                ballot_submission=ballotsub, ip_address=ip_address, tournament=t)
            return render(request, 'public_success.html', dict(success_kind="ballot"))
    else:
        form = BallotSetForm(ballotsub, password=True)

    context = {
        'form'                : form,
        'debate'              : da.debate,
        'round'               : round,
        'ballotsub'           : ballotsub,
        'adjudicator'         : adjudicator,
        'existing_ballotsubs' : da.debate.ballotsubmission_set.exclude(discarded=True).count(),
    }
    return render(request, 'public_enter_results.html', context)


@login_required
@tournament_view
def new_ballotset(request, t, debate_id):
    debate = get_object_or_404(Debate, id=debate_id)
    ip_address = get_ip_address(request)
    ballotsub = BallotSubmission(debate=debate, submitter=request.user,
                                 submitter_type=BallotSubmission.SUBMITTER_TABROOM,
                                 ip_address=ip_address)

    if not debate.adjudicators.has_chair:
        messages.error(request, "Whoops! The debate %s doesn't have a chair, "
                       "so you can't enter results for it." % debate.matchup)
        return redirect_round('results', debate.round)

    if request.method == 'POST':
        form = BallotSetForm(ballotsub, request.POST)
        if form.is_valid():
            form.save()
            ActionLogEntry.objects.log(type=ActionLogEntry.ACTION_TYPE_BALLOT_CREATE, user=request.user,
                                       ballot_submission=ballotsub, ip_address=ip_address, tournament=t)
            messages.success(request, "Ballot set for %s added." % debate.matchup)
            return redirect_round('results', debate.round)
    else:
        form = BallotSetForm(ballotsub)

    template = 'enter_results.html' if request.user.is_superuser else 'assistant_enter_results.html'
    all_ballotsubs = debate.ballotsubmission_set.order_by('version')
    if not request.user.is_superuser:
        all_ballotsubs = all_ballotsubs.exclude(discarded=True)

    context = {
        'form'             : form,
        'ballotsub'        : ballotsub,
        'debate'           : debate,
        'round'            : debate.round,
        'all_ballotsubs'   : all_ballotsubs,
        'not_singleton'    : all_ballotsubs.exists(),
        'new'              : True,
    }
    return render(request, template, context)


@login_required
@tournament_view
def ballots_status(request, t):
    # Draw Status for Tournament Homepage
    # Should be a JsonDataResponseView
    intervals = 20

    def minutes_ago(time):
        time_difference = datetime.datetime.now() - time
        minutes_ago = time_difference.days * 1440 + time_difference.seconds / 60
        return minutes_ago

    ballots = list(BallotSubmission.objects.filter(debate__round=t.current_round).order_by('timestamp'))
    debates = Debate.objects.filter(round=t.current_round).count()
    if len(ballots) is 0:
        return HttpResponse(json.dumps([]), content_type="text/json")

    start_entry = minutes_ago(ballots[0].timestamp)
    end_entry = minutes_ago(ballots[-1].timestamp)
    chunks = (end_entry - start_entry) / intervals

    stats = []
    for i in range(intervals + 1):
        time_period = (i * chunks) + start_entry
        stat = [int(time_period), debates, 0, 0]
        for b in ballots:
            if minutes_ago(b.timestamp) >= time_period:
                if b.debate.result_status == Debate.STATUS_DRAFT:
                    stat[2] += 1
                    stat[1] -= 1
                elif b.debate.result_status == Debate.STATUS_CONFIRMED:
                    stat[3] += 1
                    stat[1] -= 1
        stats.append(stat)

    return HttpResponse(json.dumps(stats), content_type="text/json")


@login_required
@tournament_view
def latest_results(request, t):
    # Latest Results for Tournament Homepage
    # Should be a JsonDataResponseView
    results_objects = []
    ballots = BallotSubmission.objects.filter(
        debate__round__tournament=t, confirmed=True).order_by(
        '-timestamp')[:15].select_related('debate')
    timestamp_template = Template("{% load humanize %}{{ t|naturaltime }}")
    for b in ballots:
        if b.ballot_set.winner == b.ballot_set.debate.aff_team:
            winner = b.ballot_set.debate.aff_team.short_name + " (Aff)"
            looser = b.ballot_set.debate.neg_team.short_name + " (Neg)"
        else:
            winner = b.ballot_set.debate.neg_team.short_name + " (Neg)"
            looser = b.ballot_set.debate.aff_team.short_name + " (Aff)"

        results_objects.append({
            'user': winner + " beat " + looser,
            'timestamp': timestamp_template.render(Context({'t': b.timestamp})),
        })

    return HttpResponse(json.dumps(results_objects), content_type="text/json")


@login_required
@round_view
def ballot_checkin(request, round):
    ballots_left = ballot_checkin_number_left(round)
    return render(request, 'ballot_checkin.html', dict(ballots_left=ballots_left))


class DebateBallotCheckinError(Exception):
    pass


def get_debate_from_ballot_checkin_request(request, round):
    # Called by the submit button on the ballot checkin form.
    # Returns the message that should go in the "success" field.
    v = request.POST.get('venue')

    try:
        venue = Venue.objects.get(name__iexact=v)
    except Venue.DoesNotExist:
        raise DebateBallotCheckinError('There aren\'t any venues with the name "' + v + '".')

    try:
        debate = Debate.objects.get(round=round, venue=venue)
    except Debate.DoesNotExist:
        raise DebateBallotCheckinError('There wasn\'t a debate in venue ' + venue.name + ' this round.')

    if debate.ballot_in:
        raise DebateBallotCheckinError('The ballot for venue ' + venue.name + ' has already been checked in.')

    return debate


def ballot_checkin_number_left(round):
    count = Debate.objects.filter(round=round, ballot_in=False).count()
    return count


@login_required
@round_view
def ballot_checkin_get_details(request, round):
    # Should be a JsonDataResponseView
    try:
        debate = get_debate_from_ballot_checkin_request(request, round)
    except DebateBallotCheckinError as e:
        data = {'exists': False, 'message': str(e)}
        return HttpResponse(json.dumps(data))

    obj = dict()

    obj['exists'] = True
    obj['venue'] = debate.venue.name
    obj['aff_team'] = debate.aff_team.short_name
    obj['neg_team'] = debate.neg_team.short_name

    adjs = debate.adjudicators
    adj_names = [adj.name for type, adj in adjs if type != DebateAdjudicator.TYPE_TRAINEE]
    obj['num_adjs'] = len(adj_names)
    obj['adjudicators'] = adj_names

    obj['ballots_left'] = ballot_checkin_number_left(round)

    return HttpResponse(json.dumps(obj))


@login_required
@round_view
def post_ballot_checkin(request, round):
    # Should be a JsonDataResponseView
    try:
        debate = get_debate_from_ballot_checkin_request(request, round)
    except DebateBallotCheckinError as e:
        data = {'exists': False, 'message': str(e)}
        return HttpResponse(json.dumps(data))

    debate.ballot_in = True
    debate.save()

    ActionLogEntry.objects.log(type=ActionLogEntry.ACTION_TYPE_BALLOT_CHECKIN,
                               user=request.user, debate=debate,
                               tournament=round.tournament)

    obj = dict()

    obj['success'] = True
    obj['venue'] = debate.venue.name
    obj['debate_description'] = debate.aff_team.short_name + " vs " + debate.neg_team.short_name

    obj['ballots_left'] = ballot_checkin_number_left(round)

    return HttpResponse(json.dumps(obj))


@cache_page(settings.PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('ballots_released')
def public_ballots_view(request, t, debate_id):
    debate = get_object_or_404(Debate, id=debate_id)
    if debate.result_status != Debate.STATUS_CONFIRMED:
        raise Http404()

    round = debate.round
    # Can't see results for current round or later
    if round.seq > round.tournament.current_round.seq or round.silent:
        if not round.tournament.pref('all_results_released'):
            raise Http404()

    ballot_submission = debate.confirmed_ballot
    if ballot_submission is None:
        raise Http404()

    ballot_set = BallotSet(ballot_submission)
    return render(request, 'public_ballot_set.html', dict(debate=debate, ballot_set=ballot_set))


@cache_page(settings.PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('public_ballots')
def public_ballot_submit(request, t):
    r = t.current_round

    das = DebateAdjudicator.objects.filter(debate__round=r).select_related('adjudicator', 'debate')

    if r.draw_status == r.STATUS_RELEASED and r.motions_good_for_public:
        return render(request, 'public_add_ballot.html', dict(das=das))
    else:
        return render(request, 'public_add_ballot_unreleased.html', dict(das=None, round=r))