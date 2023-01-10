
import asyncio
import aiohttp
from aiohttp import web
from cbpi.api.step import CBPiStep, StepResult
from cbpi.api.timer import Timer
from cbpi.api.dataclasses import Kettle, Props
from datetime import datetime
import time
from cbpi.api import *
import logging
from socket import timeout
from typing import KeysView
from cbpi.api.config import ConfigType
from cbpi.api.base import CBPiBase
from voluptuous.schema_builder import message
from cbpi.api.dataclasses import NotificationAction, NotificationType
import numpy as np
import requests
import warnings

async def setAutoMode(kettle, auto_state):
    try:
        if (kettle.instance is None or kettle.instance.state == False) and (auto_state is True):
            url = "http://127.0.0.1:" + cbpi.static_config.get('port',8000) + "/kettle/" + kettle.id + "/toggle"
            async with aiohttp.ClientSession() as session:
                async with session.post(url) as response:
                    return await response.text()            
        elif (kettle.instance.state == True) and (auto_state is False):
            await kettle.instance.stop()
    except Exception as e:
        logging.error("Failed to switch KettleLogic {} {}".format(kettle.id, e))


@parameters([Property.Kettle(label="HLT", description="HLT"),
             Property.Number(label="HLT Target Temp", configurable=True),
             Property.Kettle(label="Mash Tun", description="Mash Tun"),
             Property.Number(label="Mash Tun Target Temp", configurable=True)])

class Laurelstein_MashInStep(CBPiStep):

    async def NextStep(self, **kwargs):
        #await setAutoMode(self.hlt, False)
        await setAutoMode(self.mash_tun, False)
        await self.next()

    async def on_start(self):
        #self.port = str(self.cbpi.static_config.get('port',8000))
        self.hlt = self.get_kettle(self.props.get("HLT", None))
        self.hlt.target_temp = float(self.props.get("HLT Target Temp", 0))
        
        self.mash_tun = self.get_kettle(self.props.get("Mash Tun", None))
        self.mash_tun.target_temp = float(self.props.get("Mash Tun Target Temp", 0))
        
        await setAutoMode(hlt, True)
        await setAutoMode(mash_tun, True)
        
        self.summary = "Waiting for Target Temps..."
        
        await self.push_update()

    async def on_stop(self):
        self.summary = "Mash-In Target Temps reached."
        self.cbpi.notify(self.name, 'Mash-In Target Temps reached. Add malts and salts, and move to next step, Mash.', action=[NotificationAction("Next Step", self.NextStep)])
        await self.push_update()

    async def run(self):
        hlt_temp_reached = False
        mash_tun_temp_reached = False
        
        while self.running == True and (hlt_temp_reached == False or mash_tun_temp_reached == False):
            await asyncio.sleep(1)
            if self.get_sensor_value(self.hlt.sensor).get("value") >= self.hlt.target_temp:
                hlt_temp_reached = True
            if self.get_sensor_value(self.mash_tun.sensor).get("value") >= self.mash_tun.target_temp:
                mash_tun_temp_reached = True
            
        return StepResult.DONE
    

@parameters([Property.Number(label="Timer", description="Time in Minutes", configurable=True), 
             Property.Kettle(label="HLT", description="HLT"),
             Property.Number(label="HLT Target Temp", configurable=True),
             Property.Kettle(label="Mash Tun", description="Mash Tun"),
             Property.Number(label="Mash Tun Target Temp", configurable=True,
                             description="Mash Tun won't be direct-fired, but temp will be monitored and notifications posted if Mash Tun strays from desired temp by more than two degrees."),
             Property.Actor(label="HERMS Pump", description="HERMS Pump"),
             Property.Actor(label="Alarm",
                            description="Alarm to turn ON if Mash Tun temp strays from target temp by more than two degrees.")])

class Laurelstein_MashStep(CBPiStep):

    async def NextStep(self, **kwargs):
        #await setAutoMode(self.hlt, False)
        #await self.actor_off(self.herms_pump)
        await self.next()
        
    @action("Start Timer", [])
    async def start_timer(self):
        if self.timer.is_running is not True:
            self.cbpi.notify(self.name, 'Timer started', NotificationType.INFO)
            self.timer.start()
            self.timer.is_running = True
        else:
            self.cbpi.notify(self.name, 'Timer is already running', NotificationType.WARNING)

    @action("Add 5 Minutes to Timer", [])
    async def add_timer(self):
        if self.timer.is_running == True:
            self.cbpi.notify(self.name, '5 Minutes added', NotificationType.INFO)
            await self.timer.add(300)       
        else:
            self.cbpi.notify(self.name, 'Timer must be running to add time', NotificationType.WARNING)

    async def on_timer_done(self,timer):
        self.summary = "Mash Timer Expired."
        self.cbpi.notify(self.name, 'Mash Timer Expired.  Move to next step.',
                         action=[NotificationAction("Next Step", self.NextStep)])
        await self.push_update()
        
    async def on_timer_update(self,timer, seconds):
        self.summary = Timer.format_time(seconds)
        await self.push_update()

    async def on_start(self):        
        # PID control HLT
        self.hlt = self.get_kettle(self.props.HLT)
        self.hlt.target_temp = float(self.props.get("HLT Target Temp", 0))        
        await self.setAutoMode(self.hlt, True)
        
        # Start HERMS
        self.herms_pump = self.props.get("HERMS Pump", None)
        await self.actor_on(self.herms_pump)

        # Get Mash Target and Alarm (for monitoring)
        self.mash_tun = self.get_kettle(self.props.get("Mash Tun"))
        self.mash_tun.target_temp = float(self.props.get("Mash Tun Target Temp", 0))        
        self.alarm = self.props.get("Alarm", None)
        
        # Create Timer
        if self.timer is None:
            self.timer = Timer(int(self.props.get("Timer",0)) * 60 ,on_update=self.on_timer_update, on_done=self.on_timer_done)
        
        self.summary = "Waiting for Timer..."
        await self.push_update()

    async def on_stop(self):
        await self.timer.stop()
        self.summary = "Timer Stopped."        
        await self.push_update()

    async def reset(self):
        self.timer = Timer(int(self.props.get("Timer",0)) *60 ,on_update=self.on_timer_update, on_done=self.on_timer_done)

    async def run(self):
        last_mash_tun_alarm = 61
        while self.running == True:
            await asyncio.sleep(1)
                            
            # Start Timer if it's not running
            if self.timer.is_running is not True:
                self.timer.start()
                self.timer.is_running = True
                estimated_completion_time = datetime.fromtimestamp(time.time()+ (int(self.props.get("Timer",0)))*60)
                self.cbpi.notify(self.name, 'Timer started. Estimated completion: {}'.format(estimated_completion_time.strftime("%H:%M")), NotificationType.INFO)
        
            # Check Mash Tun Temp
            if abs(self.get_sensor_value(self.mash_tun.sensor).get("value") - self.mash_tun.target_temp) > 2:
                # only notify/alarm for 5 seconds every 1 minute, max
                if last_mash_tun_alarm > 60:
                    self.cbpi.notify(self.name, 'Mash Tun Temp has strayed from Target Temp by two degrees or more!!!')
                    if self.alarm is not None:
                         await self.actor_on(self.alarm)
                    last_mash_tun_alarm = 0
                elif last_mash_tun_alarm > 5:
                    if self.alarm is not None:
                         await self.actor_off(self.alarm)
            last_mash_tun_alarm = last_mash_tun_alarm + 1
    
        return StepResult.DONE

@parameters([Property.Kettle(label="HLT", description="HLT"),
             Property.Number(label="HLT Target Temp", configurable=True),
             Property.Kettle(label="Mash Tun", description="Mash Tun"),
             Property.Number(label="Mash Tun Target Temp", configurable=True,
                             description="Mash Tun won't be direct-fired, but this step will end when Mash Tun target temp is reached"),
             Property.Actor(label="HERMS Pump", description="HERMS Pump")])

class Laurelstein_MashOutStep(CBPiStep):

    async def NextStep(self, **kwargs):
        #await setAutoMode(self.hlt, False)
        await self.next()

    async def on_start(self):
        self.hlt = self.get_kettle(self.props.get("HLT", None))
        self.hlt.target_temp = float(self.props.get("HLT Target Temp", 0))
        
        self.mash_tun = self.get_kettle(self.props.get("Mash Tun", None))
        self.mash_tun.target_temp = float(self.props.get("Mash Tun Target Temp", 0))
        
        # PID HLT
        await setAutoMode(hlt, True)

        # Start HERMS
        self.herms_pump = self.props.get("HERMS Pump", None)
        await self.actor_on(self.herms_pump)

        self.summary = "Waiting for Mash Tun Target Temp..."
        
        await self.push_update()

    async def on_stop(self):
        self.summary = "Mash-Out Target Temp reached."
        # Stop HERMS
        await self.actor_off(self.herms_pump)        
        self.cbpi.notify(self.name, 'Mash-Out Target Temp reached. Prepare to Sparge, then move to next step.',
                         action=[NotificationAction("Next Step", self.NextStep)])
        await self.push_update()

    async def run(self):
        mash_tun_temp_reached = False
        
        while self.running == True and mash_tun_temp_reached == False:
            await asyncio.sleep(1)
            if self.get_sensor_value(self.mash_tun.sensor).get("value") >= self.mash_tun.target_temp:
                mash_tun_temp_reached = True
            
        return StepResult.DONE
    
@parameters([Property.Kettle(label="HLT", description="HLT"),
             Property.Number(label="HLT Target Temp", configurable=True),
             Property.Actor(label="HLT Sparge Pump", description="HLT Sparge Pump"),
             Property.Actor(label="Mash Tun High Float Switch", description="Mash Tun High Float Switch.  If selected, HLT Sparge Pump will only turn ON when this float switch is OFF (i.e. when Mash Tun needs make-up water)"),
             Property.Actor(label="Wort Sparge Pump", description="Wort Sparge Pump"),
             Property.Kettle(label="Boil Kettle", description="Boil Kettle"),
             Property.Number(label="Boil Kettle Target Temp", configurable=True),
             Property.Actor(label="Boil Kettle Low Float Switch", description="Boil Kettle Low Float Switch.  If selected, Boil Kettle will only start firing when this switch is ON (i.e. there is now enough wort in the Boil Kettle to heat)"),
             Property.Actor(label="Boil Kettle High Float Switch", description="Boil Kettle High Float Switch.  Sparge ends when this float switch turns ON (i.e. Boil Kettle is full)"),
             Property.Actor(label="Alarm", description="Alarm to turn ON when Sparge is complete")])

class Laurelstein_SpargeStep(CBPiStep):

    async def NextStep(self, **kwargs):
        # Alarm Off
        if self.alarm is not None:
            await self.actor_off(self.alarm)

        await self.next()

    async def on_start(self):
        self.hlt = self.get_kettle(self.props.get("HLT", None))
        self.hlt.target_temp = float(self.props.get("HLT Target Temp", 0))
        self.hlt_sparge_pump = self.props.get("HLT Sparge Pump", None)
        self.mash_tun_high_float_switch = self.props.get("Mash Tun High Float Switch", None)
        self.wort_sparge_pump = self.props.get("Wort Sparge Pump", None)
        self.boil_kettle = self.get_kettle(self.props.get("Boil Kettle", None))
        self.boil_kettle.target_temp = float(self.props.get("Boil Kettle Target Temp", 0))
        self.boil_kettle_low_float_switch = self.props.get("Boil Kettle Low Float Switch", None)
        self.boil_kettle_high_float_switch = self.props.get("Boil Kettle High Float Switch", None)
        self.alarm = self.props.get("Alarm", None)
        
        # PID HLT
        await setAutoMode(hlt, True)

        # Start Wort Sparge Pump
        await self.actor_on(self.wort_sparge_pump)

        self.summary = "Waiting for Boil Kettle to fill..."
        
        await self.push_update()

    async def on_stop(self):
        self.summary = "Boil Kettle full."
        
        # Stop HLT PID and both Pumps
        await setAutoMode(self.hlt, False)
        await self.actor_off(self.hlt_sparge_pump)
        await self.actor_off(self.wort_sparge_pump)
        
        # Alarm if specified
        if self.alarm is not None:
            await self.actor_on(self.alarm)
            
        self.cbpi.notify(self.name, 'Boil Kettle Full. Prepare to Boil, then move to next step.',
                         action=[NotificationAction("Next Step", self.NextStep)])
        await self.push_update()

    async def run(self):
        # Run until boil kettle is full
        while self.running == True and self.boil_kettle_high_float_switch.get_state == False:
            await asyncio.sleep(1)
            
            # Turn on HLT Sparge pump if Mash Tun float is low
            if self.mash_tun_high_float_switch.get_state == False:
                await self.actor_on(self.hlt_sparge_pump)
            
            # PID Boil Kettle if there's enough Wort in the Boil Kettle
            if (self.boil_kettle.instance is None or self.boil_kettle.instance.state == False):
                if self.boil_kettle_low_float_switch is not None:
                    if self.boil_kettle_low_float_switch.get_state == true:
                        await setAutoMode(self.boil_kettle, True)
                else:
                    await setAutoMode(self.boil_kettle, True)
            
        return StepResult.DONE

def setup(cbpi):
    '''
    This method is called by the server during startup 
    Here you need to register your plugins at the server

    :param cbpi: the cbpi core 
    :return: 
    '''    
    
    cbpi.plugin.register("Laurelstein_MashInStep", Laurelstein_MashInStep)
    cbpi.plugin.register("Laurelstein_MashStep", Laurelstein_MashStep)
    cbpi.plugin.register("Laurelstein_MashOutStep", Laurelstein_MashOutStep)
    cbpi.plugin.register("Laurelstein_SpargeStep", Laurelstein_SpargeStep)
   
    
    

    
