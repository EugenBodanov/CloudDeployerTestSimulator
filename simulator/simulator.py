import os
import dotenv
from nicegui import ui

from . import domain as dm
from .SimulationLoop import SimulationLoop


CONFIG_JSON_PATH = os.path.abspath(os.getenv('CONFIG_JSON_PATH', './config.json'))
HIERARCHY_JSON_PATH = os.path.abspath(os.getenv('CONFIG_HIERARCHY_JSON_PATH', './config_hierarchy.json'))   
IOT_DEVICE_JSON_PATH = os.path.abspath(os.getenv('CONFIG_IOT_JSON_PATH', './config_iot_devices.json'))


def build_row(entity: dm.Entity, component: dm.Component, attribute: dm.Attribute, sim: SimulationLoop):
    with ui.row().classes(
            'grid grid-cols-[1.5fr_1.5fr_1.5fr_1.2fr_120px_4fr_120px] items-center gap-4 border-b py-2 w-full font-sans'):

        ui.label(entity.name).classes('text-lg font-bold truncate')
        ui.label(component.name).classes('truncate text-slate-700')
        ui.label(attribute.name).classes('truncate text-slate-700')
        ui.label(attribute.dataType.value).classes('font-mono text-xs bg-slate-100 p-1 rounded text-center truncate')

        is_running = sim.is_running(attribute.id)
        status_label = ui.label('runs' if is_running else 'stopped').classes(
            'w-full text-center text-sm font-medium py-1 px-2 rounded text-white '
            + ('bg-green-500' if is_running else 'bg-red-500')
        )

        config_inputs = {}
        field_box = ui.row().classes('grid grid-cols-4 gap-2 items-center w-full')

        def render_fields():
            field_box.clear()
            config_inputs.clear()
            dtype = attribute.dataType.value

            with field_box:
                if "VECTOR" in dtype:
                    input_container = ui.row().classes('col-span-3 grid grid-cols-3 gap-1')

                    def update_vector_fields(e):
                        input_container.clear()
                        mode = e.value

                        for k in list(config_inputs.keys()):
                            if k != 'mode':
                                del config_inputs[k]

                        with input_container:
                            if mode == 'vector_custom':
                                config_inputs['vector'] = ui.input('vector').props('dense outlined')
                            elif mode == 'vector_uniform':
                                config_inputs['vec_min'] = ui.input('vec_min').props('dense outlined')
                                config_inputs['vec_max'] = ui.input('vec_max').props('dense outlined')

                    # 'api' is intentionally not offered for VECTOR attributes: the predefined
                    # provider yields a scalar. The backend keeps its VECTOR handling for later.
                    mode_select = ui.select(
                        ['vector_custom', 'vector_uniform'],
                        value='vector_custom',
                        on_change=update_vector_fields,
                    ).props('dense outlined')
                    config_inputs['mode'] = mode_select

                    with input_container:
                        config_inputs['vector'] = ui.input('vector').props('dense outlined')

                elif dtype == "STRING":
                    input_container = ui.row().classes('col-span-3 grid grid-cols-3 gap-1')

                    def update_string_fields(e):
                        input_container.clear()
                        mode = e.value

                        for k in list(config_inputs.keys()):
                            if k != 'mode':
                                del config_inputs[k]

                        with input_container:
                            if mode == 'fixed_list':
                                config_inputs['list'] = ui.input('list').props('dense outlined')
                            elif mode == 'random_string':
                                pass
                            elif mode == 'api':
                                ui.label('Uses Energy Charts renewable-share forecast (Germany)').classes('text-sm text-slate-600 col-span-3')

                    mode_select = ui.select(
                        ['fixed_list', 'random_string', 'api'],
                        value='fixed_list',
                        on_change=update_string_fields,
                    ).props('dense outlined')
                    config_inputs['mode'] = mode_select

                    with input_container:
                        config_inputs['list'] = ui.input('list').props('dense outlined')

                else:
                    input_container = ui.row().classes('col-span-3 grid grid-cols-3 gap-1')

                    def update_numeric_fields(e):
                        input_container.clear()
                        mode = e.value

                        for k in list(config_inputs.keys()):
                            if k != 'mode':
                                del config_inputs[k]

                        with input_container:
                            if mode == 'uniform':
                                config_inputs['min'] = ui.input('min').props('dense outlined')
                                config_inputs['max'] = ui.input('max').props('dense outlined')
                            elif mode == 'normal':
                                config_inputs['mean'] = ui.input('mean').props('dense outlined')
                                config_inputs['stddev'] = ui.input('stddev').props('dense outlined')
                            elif mode == 'range':
                                config_inputs['min'] = ui.input('min').props('dense outlined')
                                config_inputs['max'] = ui.input('max').props('dense outlined')
                                config_inputs['step'] = ui.input('step').props('dense outlined')
                            elif mode == 'cycle':
                                config_inputs['start'] = ui.number('Start value').props('dense outlined')
                            elif mode == 'api':
                                ui.label('Uses Energy Charts renewable-share forecast (Germany)').classes('text-sm text-slate-600 col-span-3')

                    mode_select = ui.select(
                        ['uniform', 'normal', 'range', 'cycle', 'api'],
                        value='uniform',
                        on_change=update_numeric_fields,
                    ).props('dense outlined')
                    config_inputs['mode'] = mode_select

                    with input_container:
                        config_inputs['min'] = ui.input('min').props('dense outlined')
                        config_inputs['max'] = ui.input('max').props('dense outlined')
        
        def update_field_state():
            for field in config_inputs.values():
                if hasattr(field, 'props'):
                    if sim.is_running(attribute.id):
                        field.props(add='disable')
                    else:
                        field.props(remove='disable')
        render_fields()
        update_field_state()

        def toggle():
            if sim.is_running(attribute.id):
                sim.stop_one(attribute)
                status_label.text = 'stopped'
                status_label.classes(replace='w-full text-center text-sm font-medium py-1 px-2 rounded bg-red-500 text-white')
            else:
                config = {name: field.value for name, field in config_inputs.items() if
                          name != 'mode' and field in field_box.descendants()}
                config['mode'] = config_inputs['mode'].value
                # 'start' is optional: an empty field is not sent at all, so cycle mode
                # falls back to its existing default and begins at 0
                if config.get('start') in (None, ''):
                    config.pop('start', None)
                sim.start_one(attribute, component.iotDeviceId, config)
                status_label.text = 'runs'
                status_label.classes(replace='w-full text-center text-sm font-medium py-1 px-2 rounded bg-green-500 text-white')
            update_field_state()
        ui.button('Start/Stop', on_click=toggle).props('color=primary dense outlined').classes('w-full')


twin = dm.Twin()
twin.read_from_json(CONFIG_JSON_PATH, IOT_DEVICE_JSON_PATH, HIERARCHY_JSON_PATH)
sim = SimulationLoop(topic=twin.name+"/iot-data")


@ui.page('/')
def index_page():
    with ui.card().classes('w-full p-4 bg-slate-50 mb-4'):
        ui.label(f"Digital Twin Simulation: {twin.name}").classes('text-2xl font-extrabold text-slate-800')

        with ui.row().classes('items-center gap-2'):
            ui.label('SimTime(in sec)')
            ui.number(value=sim.sim_time, min=0.1, step=0.5,
                      on_change=lambda e: setattr(sim, 'sim_time', e.value)).props('dense outlined').classes('w-32')

    with ui.row().classes(
            'grid grid-cols-[1.5fr_1.5fr_1.5fr_1.2fr_120px_4fr_120px] gap-4 font-bold border-b-2 pb-2 mb-2 w-full px-1 text-slate-600 font-sans'):
        ui.label('Entity')
        ui.label('Component')
        ui.label('Attribute')
        ui.label('Datatype')
        ui.label('Status').classes('text-center')
        ui.label('Configuration Parameters')
        ui.label('Action').classes('text-center')

    for entity, component, attribute in twin.flatten():
        build_row(entity, component, attribute, sim)


def main():
    ui.run(host="0.0.0.0", port=5000)


if __name__ in {"__main__", "__mp_main__"}:
    main()
