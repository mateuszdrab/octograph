#!/usr/bin/env python

from configparser import ConfigParser
from urllib import parse

import click
import maya
import requests
from influxdb import InfluxDBClient


def retrieve_paginated_data(
        api_key, url, from_date, to_date, page=None
):
    args = {
        'period_from': from_date,
        'period_to': to_date,
    }
    if page:
        args['page'] = page
    response = requests.get(url, params=args, auth=(api_key, ''))
    response.raise_for_status()
    data = response.json()
    results = data.get('results', [])
    if data['next']:
        url_query = parse.urlparse(data['next']).query
        next_page = parse.parse_qs(url_query)['page'][0]
        results += retrieve_paginated_data(
            api_key, url, from_date, to_date, next_page
        )
    return results


def store_series(connection, series, account_name, metrics, rate_data):

    agile_data = rate_data.get('agile_unit_rates', [])
    agile_rates = {
        point['valid_from']: point['value_inc_vat']
        for point in agile_data
    }

    def active_rate_field(measurement):
        if series == 'gas':
            return 'unit_rate'
        elif not rate_data['unit_rate_zone']:  # no low rate
            return 'unit_rate_high'

        low_start_str = rate_data['unit_rate_low_start']
        low_end_str = rate_data['unit_rate_low_end']
        low_zone = rate_data['unit_rate_zone']

        measurement_at = maya.parse(measurement['interval_start'])

        low_start = maya.when(
            measurement_at.datetime(to_timezone=low_zone).strftime(
                f'%Y-%m-%dT{low_start_str}'
            ),
            timezone=low_zone
        )
        low_end = maya.when(
            measurement_at.datetime(to_timezone=low_zone).strftime(
                f'%Y-%m-%dT{low_end_str}'
            ),
            timezone=low_zone
        )
        low_period = maya.MayaInterval(low_start, low_end)

        return \
            'unit_rate_low' if measurement_at in low_period \
            else 'unit_rate_high'

    def fields_for_measurement(measurement):
        consumption = measurement['consumption']
        conversion_factor = rate_data.get('conversion_factor', None)
        if conversion_factor:
            consumption *= conversion_factor
        rate = active_rate_field(measurement)
        rate_cost = rate_data[rate]
        cost = consumption * rate_cost
        standing_charge = rate_data['standing_charge'] / 48  # 30 minute reads
        fields = {
            'consumption': consumption,
            'cost': cost,
            'total_cost': cost + standing_charge,
            'rate': rate_cost
        }
        if agile_data:
            agile_standing_charge = rate_data['agile_standing_charge'] / 48
            agile_unit_rate = agile_rates.get(
                maya.parse(measurement['interval_start']).iso8601(),
                rate_data[rate]  # cludge, use Go rate during DST changeover
            )
            agile_cost = agile_unit_rate * consumption
            fields.update({
                'agile_rate': agile_unit_rate,
                'agile_cost': agile_cost,
                'agile_total_cost': agile_cost + agile_standing_charge,
            })
        return fields

    def tags_for_measurement(measurement):
        period = maya.parse(measurement['interval_start'])
        time = period.datetime().strftime('%H:%M')
        return {
            'active_rate': active_rate_field(measurement),
            'time_of_day': time,
            'account_name' : account_name
        }

    measurements = [
        {
            'measurement': series,
            'tags': tags_for_measurement(measurement),
            'time': measurement['interval_start'],
            'fields': fields_for_measurement(measurement),
        }
        for measurement in metrics
    ]
    connection.write_points(measurements)


def get_latest_value_inc_vat(from_iso, to_iso, e_sc_url, api_key):
    args = {
        'period_from': from_iso,
        'period_to': to_iso,
    }
    response = requests.get(e_sc_url, params=args, auth=(api_key, ''))
    response.raise_for_status()
    data = response.json()
    retrieved_standing_charge = data.get('results', [])[0]["value_inc_vat"]
    return retrieved_standing_charge


@click.command()
@click.option(
    '--config-file',
    default="octograph.ini",
    type=click.Path(exists=True, dir_okay=True, readable=True),
)
@click.option('--from-date', default='yesterday midnight', type=click.STRING)
@click.option('--to-date', default='today midnight', type=click.STRING)
def cmd(config_file, from_date, to_date):

    config = ConfigParser()
    config.read(config_file)

    influx = InfluxDBClient(
        host=config.get('influxdb', 'host', fallback="localhost"),
        port=config.getint('influxdb', 'port', fallback=8086),
        username=config.get('influxdb', 'user', fallback=""),
        password=config.get('influxdb', 'password', fallback=""),
        database=config.get('influxdb', 'database', fallback="energy"),
    )

    api_key = config.get('octopus', 'api_key')
    account_name = config.get('octopus', 'account_name', fallback=None)
    if not api_key:
        raise click.ClickException('No Octopus API key set')

    timezone = config.get('electricity', 'unit_rate_time_zone', fallback="Europe/London")
    from_iso = maya.when(from_date, timezone=timezone).iso8601()
    to_iso = maya.when(to_date, timezone=timezone).iso8601()

    e_mpan = config.get('electricity', 'mpan', fallback=None)
    e_serial = config.get('electricity', 'serial_number', fallback=None)
    if not e_mpan or not e_serial:
        raise click.ClickException('No electricity meter identifiers')
    e_url = 'https://api.octopus.energy/v1/electricity-meter-points/' \
            f'{e_mpan}/meters/{e_serial}/consumption/'
    e_product_code = config.get('electricity', 'product_code', fallback="AGILE-FLEX-22-11-25")
    e_tariff_code = config.get('electricity', 'tariff_code', fallback="E-1R-AGILE-FLEX-22-11-25-C")
    agile_url = f'https://api.octopus.energy/v1/products/{e_product_code}/electricity-tariffs/{e_tariff_code}/standard-unit-rates/'

    e_sc_url = f'https://api.octopus.energy/v1/products/{e_product_code}/electricity-tariffs/{e_tariff_code}/standing-charges/'
    e_retrieved_standing_charge = get_latest_value_inc_vat(from_iso, to_iso, e_sc_url, api_key)

    g_ignore = config.getboolean('gas', 'ignore', fallback=False)
    g_mprn = config.get('gas', 'mprn', fallback=None)
    g_serial = config.get('gas', 'serial_number', fallback=None)
    g_meter_type = config.get('gas', 'meter_type', fallback=1)
    g_vcf = config.get('gas', 'volume_correction_factor', fallback=1.02264)
    g_cv = config.get('gas', 'calorific_value', fallback=40)
    if (not g_mprn or not g_serial) and not g_ignore:
        raise click.ClickException('No gas meter identifiers')
    elif not g_ignore:
        g_product_code = config.get('gas', 'product_code', fallback="VAR-22-10-01")
        g_tariff_code = config.get('gas', 'tariff_code', fallback="G-1R-VAR-22-10-01-C")
        g_url = 'https://api.octopus.energy/v1/gas-meter-points/' \
                f'{g_mprn}/meters/{g_serial}/consumption/'
        g_sc_url = f'https://api.octopus.energy/v1/products/{g_product_code}/gas-tariffs/{g_tariff_code}/standing-charges/'
        g_retrieved_standing_charge = get_latest_value_inc_vat(from_iso, to_iso, g_sc_url, api_key)
        g_unit_url = f'https://api.octopus.energy/v1/products/{g_product_code}/gas-tariffs/{g_tariff_code}/standard-unit-rates/'
        g_unit_cost = get_latest_value_inc_vat(from_iso, to_iso, g_unit_url, api_key)

    rate_data = {
        'electricity': {
            'standing_charge': config.getfloat(
                'electricity', 'standing_charge', fallback=e_retrieved_standing_charge
            ),
            'unit_rate_high': config.getfloat(
                'electricity', 'unit_rate_high', fallback=0.0
            ),
            'unit_rate_low': config.getfloat(
                'electricity', 'unit_rate_low', fallback=0.0
            ),
            'unit_rate_low_start': config.get(
                'electricity', 'unit_rate_low_start', fallback="00:00"
            ),
            'unit_rate_low_end': config.get(
                'electricity', 'unit_rate_low_end', fallback="00:00"
            ),
            'unit_rate_zone': timezone,
            'agile_standing_charge': config.getfloat(
                'electricity', 'agile_standing_charge', fallback=e_retrieved_standing_charge
            ),
            'agile_unit_rates': [],
        }
    }
    
    if not g_ignore:
        rate_data.update({'gas': {
            'standing_charge': config.getfloat(
                'gas', 'standing_charge', fallback=g_retrieved_standing_charge
            ),
            'unit_rate': config.getfloat('gas', 'unit_rate', fallback=g_unit_cost),
            # SMETS1 meters report kWh, SMET2 report m^3 and need converting to kWh first
            'conversion_factor': (float(g_vcf) * float(g_cv)) / 3.6 if int(g_meter_type) > 1 else None,
        }})

    click.echo(
        f'Retrieving electricity data for {from_iso} until {to_iso}...',
        nl=False
    )
    e_consumption = retrieve_paginated_data(
        api_key, e_url, from_iso, to_iso
    )
    click.echo(f' {len(e_consumption)} readings.')
    click.echo(
        f'Retrieving Agile rates for {from_iso} until {to_iso}...',
        nl=False
    )
    rate_data['electricity']['agile_unit_rates'] = retrieve_paginated_data(
        api_key, agile_url, from_iso, to_iso
    )
    click.echo(f' {len(rate_data["electricity"]["agile_unit_rates"])} rates.')
    store_series(influx, 'electricity', account_name, e_consumption, rate_data['electricity'])

    if not g_ignore:
        click.echo(
            f'Retrieving gas data for {from_iso} until {to_iso}...',
            nl=False
        )
        g_consumption = retrieve_paginated_data(
            api_key, g_url, from_iso, to_iso
        )
        click.echo(f' {len(g_consumption)} readings.')
        store_series(influx, 'gas', account_name, g_consumption, rate_data['gas'])


if __name__ == '__main__':
    cmd()