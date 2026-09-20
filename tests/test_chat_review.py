"""Chat capture and final verdict checks, without a running game or input hooks."""

import json
from unittest.mock import Mock

import pytest
import pyinsim

from simulation_tests import chat_review, packet_dump, run_scenario, scenario
from simulation_tests.trace_format import TraceWriter
from tests.fake_lfs import mso


def record(text='Ungültiger Parameter', event='MSO', **fields):
    return {'t': 1.5, 'ev': event, 'd': {'text': text, 'UserType': 0, **fields}}


def check(records, **kwargs):
    return chat_review.review(records, {'packets': list(chat_review.PACKETS)},
                              kwargs.get('end', {'dropped': 0}))


@pytest.mark.parametrize('text', ['Ungültiger Parameter', '^1Invalid parameter',
                                  'WARNING: something happened', 'Datei nicht gefunden'])
def test_diagnostics_fail_and_keep_timestamp(text):
    data = packet_dump.packet_to_dict('MSO', pyinsim.IS_MSO().unpack(mso(text.encode('cp1252'))))
    result = check([{'t': 2.5, 'ev': 'MSO', 'd': data}])
    assert result['status'] == 'failed'
    assert result['diagnostics'][0]['t'] == 2.5
    assert data['raw_text_hex'] == text.encode('cp1252').hex()


def test_mso_preserves_code_page_and_decodes_inline_switches():
    raw = 'Ошибка'.encode('cp1251') + b' ^L\xfc'
    packet = pyinsim.IS_MSO().unpack(mso(raw, code_page=5))
    assert packet.MSOData == 5
    assert packet_dump.packet_to_dict('MSO', packet)['text'] == 'Ошибка ü'


def test_dbcs_trailing_caret_is_not_a_colour_escape():
    # ソ ends in 0x5e under cp932. The following 1 is literal, not ^1.
    assert chat_review.message_text('ソ1'.encode('cp932'), 4) == 'ソ1'


def test_existing_trace_hex_is_used_for_german_diagnostic():
    old = {'t': 3, 'ev': 'MSO', 'd': {'UserType': 0, 'Msg': {
        'hex': '556e67fc6c746967657220506172616d65746572', 'text': 'damaged display'}}}
    assert check([old])['diagnostics'][0]['text'] == 'Ungültiger Parameter'


@pytest.mark.parametrize('user_type', [1, 2, 3])
def test_user_chat_is_recorded_without_becoming_a_system_error(user_type):
    result = check([record(UserType=user_type)])
    assert result['status'] == 'passed'
    assert len(result['messages']) == 1


def test_unknown_system_messages_require_review():
    assert check([record('Something new')])['status'] == 'needs_review'


@pytest.mark.parametrize('text', ['Autocross: 1 Kontrollpunkt', 'Layout: AU4_fcw_2lead',
                                  'AI 1^L hat das Rennen gewonnen'])
def test_observed_informational_messages_are_not_warnings(text):
    data = packet_dump.packet_to_dict('MSO', pyinsim.IS_MSO().unpack(mso(text.encode())))
    assert check([{'t': 1, 'ev': 'MSO', 'd': data}])['status'] == 'passed'


@pytest.mark.parametrize('result,status', [(1, 'passed'), (2, 'failed'), (3, 'failed'),
                                          (0, 'needs_review')])
def test_command_report_result_is_checked(result, status):
    assert check([record('/command', event='ACR', Result=result)])['status'] == status


def test_missing_subscription_or_dropped_trace_cannot_pass_as_silent_chat():
    assert check([])['status'] == 'passed'
    assert check([], end=None)['status'] == 'incomplete'
    assert check([], end={'dropped': 1})['status'] == 'incomplete'
    assert chat_review.review([], {'packets': ['MCI']}, {})['status'] == 'incomplete'


def test_scenario_arguments_include_chat_even_for_custom_tracers():
    spec = {'tracer': {**scenario.DEFAULT_TRACER, 'packets': ['STA']}}
    args = scenario.tracer_argv(spec, 'trace.jsonl', 123)
    assert set(args[args.index('--packets') + 1].split(',')) == {'STA', *chat_review.PACKETS}


@pytest.mark.parametrize('text,incoming,expected', [
    ('Ungültiger Parameter', 0, 9), ('Ungültiger Parameter', 6, 6),
    ('Something new', 0, 10), ('Autocross: 1 Kontrollpunkt', 0, 0)])
def test_finish_enforces_chat_check_even_without_printed_summary(tmp_path, text, incoming, expected):
    trace = str(tmp_path / 'trace.jsonl')
    writer = TraceWriter(trace)
    writer.write('tracer', 'meta', {'packets': list(chat_review.PACKETS)}, t=0)
    writer.write('insim', 'MSO', {'UserType': 0, 'text': text}, t=1)
    writer.write('tracer', 'end', {'dropped': 0}, t=2)
    writer.close()
    process = Mock(returncode=0)
    code = run_scenario._finish(process, Mock(), Mock(), str(tmp_path),
                                {'functional_verdict': 'not_evaluated'}, incoming,
                                trace_path=trace, print_summary=False)
    saved = json.loads((tmp_path / 'run.json').read_text(encoding='utf-8'))
    assert code == saved['exit_code'] == expected
    assert saved['functional_verdict'] == 'not_evaluated'
    assert saved['chat_check']['messages'][0]['text'] == text
