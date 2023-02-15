
import asyncio
import aiohttp
from aiohttp import web
from cbpi.api.step import CBPiStep, StepResult
#from cbpi.api.timer import Timer
from cbpi.api.dataclasses import Kettle, Props
from datetime import datetime
import time
import math
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

        
@parameters([Property.Text(label="Notification", configurable=True, description="Text for notification"),
             Property.Actor(label="Alarm", description="Alarm to turn on along with the notification (optional)."),
             Property.Select(label="AutoNext", options=["Yes","No"], description="Automatically move to next step (Yes) or pause after Notification (No)"),
             Property.Actor(label="Input", description="Input actor for moving to next step")
             ])
class Laurelstein_NotificationStep(CBPiStep):

    async def on_start(self):
        self.AutoNext = False if self.props.get("AutoNext", "No") == "No" else True
        self.alarm = self.props.get("Alarm", None)
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)
        await self.push_update()

    async def on_stop(self):
        self.summary = ""
        await toggle_off(self, self.alarm)
        await self.push_update()

    async def run(self):
        last_notify = 10
        self.summary = self.props.get("Notification","")
        self.cbpi.notify(self.name, self.props.get("Notification",""), NotificationType.INFO)
        await toggle_on(self, self.alarm)
        
        while self.running == True:
            if self.AutoNext == True or checkActorOn(self.input_actor):
                await self.next()
            elif last_notify >= 10:
                self.cbpi.notify(self.name, self.props.get("Notification",""), NotificationType.INFO)
                last_notify = 0
                await self.push_update()
            last_notify = last_notify + 1
            await asyncio.sleep(1)

        return StepResult.DONE

@parameters([Property.Number(label="Timer", description="Time in Minutes", configurable=True),
             Property.Text(label="Notification", configurable=True, description="Text for notification"),
             Property.Actor(label="Alarm", description="Alarm to turn on along with the notification (optional)."),
             Property.Select(label="AutoNext", options=["Yes","No"], description="Automatically move to next step (Yes) or pause after Notification (No)"),
             Property.Actor(label="Input", description="Input actor for moving to next step")
             ])
class Laurelstein_TimerStep(CBPiStep):

    @action("Add 1 Minute to Timer", [])
    async def add_one_timer(self):
        if self.timer is not None:
            self.cbpi.notify(self.name, '1 Minute added', NotificationType.INFO)
            await self.timer.add(60)

    @action("Add 5 Minutes to Timer", [])
    async def add_five_timer(self):
        if self.timer is not None:
            self.cbpi.notify(self.name, '5 Minutes added', NotificationType.INFO)
            await self.timer.add(300)       

    async def on_timer_done(self, timer):
        if self.stopped == False:
            self.timer_expired = True
            self.summary = "Timer Expired.  Move to next step"
        await self.push_update()
        
    async def on_timer_update(self, timer, seconds):
        self.summary = format_time(seconds)
        await self.push_update()

    async def on_start(self):        
        self.stopped = False
        self.timer_expired = False
        self.AutoNext = False if self.props.get("AutoNext", "No") == "No" else True
        self.alarm = self.props.get("Alarm", None)
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)
        
        if self.timer is None:
            logging.info("Instantiating new Timer")
            self.timer = Timer(int(self.props.get("Timer",0)) * 60, on_update=self.on_timer_update, on_done=self.on_timer_done)

        logging.info("starting timer")
        self.timer.start()
        self.cbpi.notify(self.name, 'Timer started', NotificationType.INFO)                    
        await self.push_update()

    async def on_stop(self):
        logging.info("stopping timer")
        self.stopped = True
        await self.timer.stop()
        await toggle_off(self, self.alarm)
        await self.push_update()

    async def reset(self):
        self.summary = ""
        self.timer = Timer(int(self.props.get("Timer",0)) *60 ,on_update=self.on_timer_update, on_done=self.on_timer_done)

    async def run(self):
        last_warning = 10
        while self.running == True:
            if self.timer_expired == True:
                # Timer Expired
                await toggle_on(self, self.alarm)
                if self.AutoNext or checkActorOn(self.input_actor):
                    self.summary = ""
                    await self.next()
                elif last_warning >= 10:
                    self.cbpi.notify(self.name, 'Timer Expired.  Move to next step.', NotificationType.SUCCESS)
                    last_warning = 0
                last_warning = last_warning + 1
            await asyncio.sleep(1)
        return StepResult.DONE

@parameters([Property.Kettle(label="HLT", description="HLT"),
             Property.Number(label="HLT Target Temp", configurable=True),
             Property.Kettle(label="Mash Tun", description="Mash Tun"),
             Property.Number(label="Mash Tun Target Temp", configurable=True),
             Property.Actor(label="HERMS Pump", description="HERMS Pump"),
             Property.Actor(label="Alarm", description="Alarm to turn on along with the notification (optional)."),
             Property.Actor(label="Input", description="Input actor for moving to next step")
             ])
class Laurelstein_MashInStep(CBPiStep):
    
    async def on_start(self):
        self.summary = "Waiting for Target Temps..."
        self.hlt = self.get_kettle(self.props.get("HLT", None))
        self.hlt.target_temp = float(self.props.get("HLT Target Temp", 0))
        self.mash_tun = self.get_kettle(self.props.get("Mash Tun", None))
        self.mash_tun.target_temp = float(self.props.get("Mash Tun Target Temp", 0))
        self.herms_pump = self.props.get("HERMS Pump", None)
        self.alarm = self.props.get("Alarm", None)
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)
        self.target_reached = False
            
        await setAutoMode(self.cbpi, self.hlt, True)
        await setAutoMode(self.cbpi, self.mash_tun, True)        
        await self.push_update()

    async def on_stop(self):
        self.summary = ""
        self.target_reached = False
        # turn off the mash tun kettle
        await setAutoMode(self.cbpi, self.mash_tun, False)
        await toggle_off(self, self.alarm)
        await self.push_update()

    async def run(self):
        last_alarm = 10
        while self.running == True:
            
            if self.get_sensor_value(self.hlt.sensor).get("value") >= self.mash_tun.target_temp:
                await toggle_on(self, self.herms_pump)
            
            if self.get_sensor_value(self.hlt.sensor).get("value") >= self.hlt.target_temp and self.get_sensor_value(self.mash_tun.sensor).get("value") >= self.mash_tun.target_temp:
                self.target_reached = True
                self.summary = "Target Temps reached."
                if last_alarm >= 10:
                    await toggle_on(self, self.alarm)
                    self.cbpi.notify(self.name, 'Target Temps reached. Add malts and salts, and move to next step.', NotificationType.SUCCESS)
                    last_alarm = 0                    

            if self.target_reached and checkActorOn(self.input_actor):
                    await self.next()
            
            await self.push_update()
            last_alarm = last_alarm + 1            
            
            await asyncio.sleep(1)
                
        return StepResult.DONE
    

@parameters([Property.Number(label="Timer", description="Time in Minutes", configurable=True), 
             Property.Kettle(label="HLT", description="HLT"),
             Property.Number(label="HLT Target Temp", configurable=True),
             Property.Kettle(label="Mash Tun", description="Mash Tun"),
             Property.Number(label="Mash Tun Target Temp", configurable=True,
                             description="Mash Tun won't be direct-fired, but temp will be monitored and notifications posted if Mash Tun strays from desired temp by more than two degrees."),
             Property.Actor(label="HERMS Pump", description="HERMS Pump"),
             Property.Actor(label="Alarm", description="Alarm to sound when Mash timer is expired."),
             #Property.Number(label="Temp Tolerance", configurable=True, description="Number of degrees outside of Mash Tun target in which to Alarm"),
             Property.Select(label="AutoNext", options=["Yes","No"], description="Automatically move to next step (Yes) or pause after Notification (No)"),
             Property.Actor(label="Input", description="Input actor for moving to next step")
             ])

class Laurelstein_MashStep(CBPiStep):
    
    @action("Add 1 Minute to Timer", [])
    async def add_one_timer(self):
        if self.timer is not None:
            self.cbpi.notify(self.name, '1 Minute added', NotificationType.INFO)
            await self.timer.add(60)

    @action("Add 5 Minutes to Timer", [])
    async def add_timer(self):
        if self.timer is not None:
            self.cbpi.notify(self.name, '5 Minutes added', NotificationType.INFO)
            await self.timer.add(300)       

    async def on_timer_done(self, timer):
        logging.info("on_timer_done called, self.stopped == " + str(self.stopped))
        if self.stopped == False:
            self.timer_expired = True
        await self.push_update()
        
    async def on_timer_update(self, timer, seconds):
        self.summary = format_time(seconds)
        await self.push_update()
        
    async def on_start(self):        
        logging.info("on_start called")
        self.stopped = False
        self.timer_expired = False
        #self.temp_tolerance = float(self.props.get("Temp Tolerance", None))
        self.AutoNext = False if self.props.get("AutoNext", "No") == "No" else True
        self.alarm = self.props.get("Alarm", None)
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)
        
        # PID control HLT
        self.hlt = self.get_kettle(self.props.HLT)
        self.hlt.target_temp = float(self.props.get("HLT Target Temp", 0))        
        await setAutoMode(self.cbpi, self.hlt, True)
        
        # Start HERMS
        self.herms_pump = self.props.get("HERMS Pump", None)
        await toggle_on(self, self.herms_pump)

        # Get Mash Target and Alarm (for monitoring)
        self.mash_tun = self.get_kettle(self.props.get("Mash Tun"))
        self.mash_tun.target_temp = float(self.props.get("Mash Tun Target Temp", 0))        
        await setAutoMode(self.cbpi, self.mash_tun, False)        
        
        # Start timer
        if self.timer is None:
            logging.info("Instantiating new Timer")
            self.timer = Timer(int(int(self.props.get("Timer",0)) * 60), on_update=self.on_timer_update, on_done=self.on_timer_done)

        logging.info("starting timer")
        self.timer.start()
        self.cbpi.notify(self.name, 'Timer started', NotificationType.INFO)                    
        await self.push_update()

    async def on_stop(self):
        logging.info("stopping timer")
        self.stopped = True
        await self.timer.stop()
        await toggle_off(self, self.alarm)
        await self.push_update()

    async def reset(self):
        logging.info("resetting timer")
        self.summary = ""
        self.timer = Timer(int(self.props.get("Timer",0)) *60 ,on_update=self.on_timer_update, on_done=self.on_timer_done)

    async def run(self):
        last_temp_warning = 10
        last_timer_warning = 10
        while self.running == True:
            if self.timer_expired == True:
                # Timer Expired
                await toggle_on(self, self.alarm)
                if self.AutoNext or checkActorOn(self.input_actor):
                    self.summary = ""
                    await self.next()
                elif last_timer_warning >= 10:
                    self.cbpi.notify(self.name, 'Timer Expired.  Move to next step.', NotificationType.SUCCESS)
                    last_timer_warning = 0
                last_timer_warning = last_timer_warning + 1
            '''    
            if self.temp_tolerance is not None and abs(self.get_sensor_value(self.mash_tun.sensor).get("value") - self.mash_tun.target_temp) > self.temp_tolerance:
                if last_temp_warning >= 10:
                    self.cbpi.notify(self.name, 'Mash Tun Temp has strayed from Target Temp!', NotificationType.WARNING)
                    await toggle_on(self, self.alarm)
                    last_temp_warning = 0
            last_temp_warning = last_temp_warning + 1
            
            # turn off alarm if input is pressed
            if checkActorOn(self.input_actor): 
                await toggle_off(self, self.alarm)
            '''    
            await asyncio.sleep(1)

        return StepResult.DONE

@parameters([Property.Kettle(label="HLT", description="HLT"),
             Property.Number(label="HLT Target Temp", configurable=True),
             Property.Kettle(label="Mash Tun", description="Mash Tun"),
             Property.Number(label="Mash Tun Target Temp", configurable=True,
                             description="Mash Tun won't be direct-fired, but this step will end when Mash Tun target temp is reached"),
             Property.Actor(label="HERMS Pump", description="HERMS Pump"),
             Property.Actor(label="Alarm", description="Alarm to sound when Mash Tun target temp is reached."),
             Property.Actor(label="Input", description="Input actor for moving to next step")
             ])

class Laurelstein_MashOutStep(CBPiStep):
    
    async def on_start(self):
        self.summary = "Waiting for Mash Tun Target Temp..."
        self.hlt = self.get_kettle(self.props.get("HLT", None))
        self.hlt.target_temp = float(self.props.get("HLT Target Temp", 0))
        self.mash_tun = self.get_kettle(self.props.get("Mash Tun", None))
        self.mash_tun.target_temp = float(self.props.get("Mash Tun Target Temp", 0))
        self.herms_pump = self.props.get("HERMS Pump", None)
        self.alarm = self.props.get("Alarm", None)
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)
        
        # PID control HLT and start up HERMS pump
        await setAutoMode(self.cbpi, self.hlt, True)
        await setAutoMode(self.cbpi, self.mash_tun, False)
        await toggle_on(self, self.herms_pump)        
        await self.push_update()

    async def on_stop(self):
        self.summary = ""
        # Stop HERMS
        await toggle_off(self, self.herms_pump)        
        await toggle_off(self, self.alarm)
        await self.push_update()

    async def run(self):
        last_warning = 10
        while self.running == True:
            if self.get_sensor_value(self.mash_tun.sensor).get("value") >= self.mash_tun.target_temp:
                self.summary = "Mash-Out Target Temp reached."
                await toggle_on(self, self.alarm)
                await toggle_off(self, self.herms_pump)        
                if last_warning >= 10:
                    self.cbpi.notify(self.name, 'Target Temp reached. Prepare to Sparge, then move to next step.', NotificationType.SUCCESS)
                    last_warning = 0
                if checkActorOn(self.input_actor):
                    self.summary = ""
                    await self.next()
            last_warning = last_warning + 1
            await asyncio.sleep(1)
            
        return StepResult.DONE
    
@parameters([Property.Kettle(label="HLT", description="HLT"),
             Property.Number(label="HLT Target Temp", configurable=True),
             Property.Actor(label="HLT Sparge Pump", description="HLT Sparge Pump"),
             Property.Actor(label="Wort Sparge Pump", description="Wort Sparge Pump"),
             Property.Kettle(label="Boil Kettle", description="Boil Kettle"),
             Property.Number(label="Boil Kettle Target Temp", configurable=True),
             Property.Actor(label="Input", description="Input actor for moving to next step")
             ])

class Laurelstein_SpargeWithHardwiredFloatsStep(CBPiStep):
    
    async def on_start(self):
        self.hlt = self.get_kettle(self.props.get("HLT", None))
        self.hlt.target_temp = float(self.props.get("HLT Target Temp", 0))
        self.hlt_sparge_pump = self.props.get("HLT Sparge Pump", None)
        self.wort_sparge_pump = self.props.get("Wort Sparge Pump", None)
        self.boil_kettle = self.get_kettle(self.props.get("Boil Kettle", None))
        self.boil_kettle.target_temp = float(self.props.get("Boil Kettle Target Temp", 0))
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)
        self.safety_timer = 0
        
        self.summary = "When Boil Kettle is full, move to next step..."
        await setAutoMode(self.cbpi, self.hlt, True)
        await setAutoMode(self.cbpi, self.boil_kettle, True)
        await toggle_on(self, self.hlt_sparge_pump)        
        await toggle_on(self, self.wort_sparge_pump)        
        await self.push_update()

    async def on_stop(self):
        self.summary = ""
        self.safety_timer = 0
        # Stop HLT PID and both Pumps
        await setAutoMode(self.cbpi, self.hlt, False)
        await setAutoMode(self.cbpi, self.boil_kettle, False)
        await toggle_off(self, self.hlt_sparge_pump)
        await toggle_off(self, self.wort_sparge_pump)
        await self.push_update()

    async def run(self):
        while self.running == True:
            if self.safety_timer > 10 and checkActorOn(self.input_actor):
                self.summary = ""
                await self.next()
            self.safety_timer = self.safety_timer + 1
            await asyncio.sleep(1)

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
             Property.Actor(label="Alarm", description="Alarm to turn ON when Sparge is complete"),
             Property.Actor(label="Input", description="Input actor for moving to next step")
             ])

class Laurelstein_SpargeStep(CBPiStep):
    
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
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)
        
        self.summary = "Waiting for Boil Kettle to fill..."
        await setAutoMode(self.cbpi, self.hlt, True)
        await toggle_on(self, self.wort_sparge_pump)        
        await self.push_update()

    async def on_stop(self):
        self.summary = ""
        # Stop HLT PID and both Pumps
        await setAutoMode(self.cbpi, self.hlt, False)
        await setAutoMode(self.cbpi, self.boil_kettle, False)
        await toggle_off(self, self.hlt_sparge_pump)
        await toggle_off(self, self.wort_sparge_pump)
        await toggle_off(self, self.alarm)
        await self.push_update()

    async def run(self):
        last_warning = 10
        while self.running == True:
            if self.boil_kettle_high_float_switch.instance.state == True:
                self.summary = "Boil Kettle Full. Prepare to Boil, then move to next step."
            
                # Stop HLT PID and both Pumps
                await setAutoMode(self.cbpi, self.hlt, False)
                await toggle_off(self, self.hlt_sparge_pump)
                await toggle_off(self, self.wort_sparge_pump)
                        
                if last_warning >= 10:
                    await toggle_on(self, self.alarm)
                    self.cbpi.notify(self.name, 'Boil Kettle Full. Prepare to Boil, then move to next step.', NotificationType.SUCCESS)
                    last_warning = 0;
                last_warning = last_warning + 1
                
                if checkActorOn(self.input_actor):
                    self.summary = ""
                    await self.next()
            else:
                # Turn on HLT Sparge pump if Mash Tun float is low
                if self.mash_tun_high_float_switch is None or self.mash_tun_high_float_switch.instance.state == False:
                    await toggle_on(self, self.hlt_sparge_pump)
            
                # PID Boil Kettle if there's enough Wort in the Boil Kettle
                if (self.boil_kettle_low_float_switch is None or self.boil_kettle_low_float_switch.instance.state == True) and (self.boil_kettle.instance is None or self.boil_kettle.instance.state == False):
                    await setAutoMode(self.cbpi, self.boil_kettle, True)

            await asyncio.sleep(1)

        return StepResult.DONE

@parameters([Property.Number(label="Timer", description="Time in Minutes", configurable=True), 
             Property.Actor(label="Boil Kettle Burner", description="Boil Kettle Burner, will turn on during boil."),
             Property.Actor(label="Alarm", description="Alarm to sound when boil timer is expired."),
             Property.Select(label="AutoNext", options=["Yes","No"], description="Automatically move to next step (Yes) or pause after Notification (No)"),
             Property.Actor(label="Input", description="Input actor for moving to next step"),
             Property.Number("Hop_1", configurable = True, description="First Hop alert (minutes before finish)"),
             Property.Text("Hop_1_text", configurable = True, description="First Hop alert text"),
             Property.Number("Hop_2", configurable=True, description="Second Hop alert (minutes before finish)"),
             Property.Text("Hop_2_text", configurable = True, description="Second Hop alert text"),
             Property.Number("Hop_3", configurable=True, description="Third Hop alert (minutes before finish)"),
             Property.Text("Hop_3_text", configurable = True, description="Third Hop alert text"),
             Property.Number("Hop_4", configurable=True, description="Fourth Hop alert (minutes before finish)"),
             Property.Text("Hop_4_text", configurable = True, description="Fourth Hop alert text"),
             Property.Number("Hop_5", configurable=True, description="Fifth Hop alert (minutes before finish)"),
             Property.Text("Hop_5_text", configurable = True, description="Fifth Hop alert text"),
             Property.Number("Hop_6", configurable=True, description="Sixth Hop alert (minutes before finish)"),
             Property.Text("Hop_6_text", configurable = True, description="Sixth Hop alert text")])

class Laurelstein_BoilStep(CBPiStep):
    
    @action("Add 1 Minute to Timer", [])
    async def add_one_timer(self):
        if self.timer is not None:
            self.cbpi.notify(self.name, '1 Minute added', NotificationType.INFO)
            await self.timer.add(60)

    @action("Add 5 Minutes to Timer", [])
    async def add_timer(self):
        if self.timer is not None:
            self.cbpi.notify(self.name, '5 Minutes added', NotificationType.INFO)
            await self.timer.add(300)       

    async def on_timer_done(self, timer):
        logging.info("on_timer_done called, self.stopped == " + str(self.stopped))
        if self.stopped == False:
            self.timer_expired = True
        await self.push_update()

    async def on_timer_update(self, timer, seconds):
        self.summary = format_time(seconds)
        self.remaining_seconds = seconds
        await self.push_update()
        
    async def on_start(self):        
        logging.info("on_start called")
        self.stopped = False
        self.timer_expired = False
        self.AutoNext = False if self.props.get("AutoNext", "No") == "No" else True
        self.alarm = self.props.get("Alarm", None)
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)

        self.burner = self.props.get("Boil Kettle Burner", None)
        await self.actor_on(self.burner)

        self.hops_added=["","","","","",""]
        self.remaining_seconds = None
                
        # Start timer
        if self.timer is None:
            logging.info("Instantiating new Timer")
            self.timer = Timer(int(int(self.props.get("Timer",0)) * 60), on_update=self.on_timer_update, on_done=self.on_timer_done)

        logging.info("starting timer")
        self.timer.start()
        self.cbpi.notify(self.name, 'Boil Timer started', NotificationType.INFO)                    
        await self.push_update()

    async def on_stop(self):
        logging.info("stopping timer")
        self.stopped = True
        await self.timer.stop()
        await self.actor_off(self.burner)
        await toggle_off(self, self.alarm)
        await self.push_update()

    async def reset(self):
        logging.info("resetting timer")
        self.summary = ""
        self.timer = Timer(int(self.props.get("Timer",0)) *60 ,on_update=self.on_timer_update, on_done=self.on_timer_done)

    async def check_hop_timer(self, number, value, text):
        if value is not None and self.hops_added[number-1] is not True:
            if self.remaining_seconds != None and self.remaining_seconds <= (int(value) * 60 + 1):
                await toggle_on(self, self.alarm)
                self.hops_added[number-1] = True
                if text is not None and text != "":
                    self.cbpi.notify('Hop Alert', "Please add %s (%s)" % (text, number), NotificationType.INFO)
                else:
                    self.cbpi.notify('Hop Alert', "Please add Hop %s" % number, NotificationType.INFO)

    async def run(self):
        last_timer_warning = 10
        while self.running == True:
            if self.timer_expired == True:
                # Stop Boil Kettle Burner
                if self.get_actor_state(self.burner) == True:
                    await self.actor_off(self.burner)
        
                if self.AutoNext or checkActorOn(self.input_actor):
                    self.summary = ""
                    await self.next()
                elif last_timer_warning >= 10:
                    await toggle_on(self, self.alarm)
                    self.cbpi.notify(self.name, 'Boil Timer Expired.  Move to next step.', NotificationType.SUCCESS)
                    last_timer_warning = 0
                last_timer_warning = last_timer_warning + 1
            else:
                # Start Boil Kettle Burner
                if self.get_actor_state(self.burner) == False:
                    await self.actor_on(self.burner)
                
                # Check hop alerts
                for x in range(1, 6):
                    await self.check_hop_timer(x, self.props.get("Hop_%s" % x, None), self.props.get("Hop_%s_text" % x, None))
        
                # Turn off any hop alarm if input is pressed          
                if checkActorOn(self.input_actor):
                    await toggle_off(self, self.alarm)

            await asyncio.sleep(1)

        return StepResult.DONE

@parameters([Property.Kettle(label="Boil Kettle", description="Boil Kettle"),
             Property.Number(label="Target Temp", configurable=True),
             Property.Actor(label="Wort Pump", description="Wort Pump"),
             Property.Actor(label="Alarm", description="Alarm to turn on along with the notification (optional)."),
             Property.Actor(label="Input", description="Input actor for moving to next step")])

class Laurelstein_CooldownStep(CBPiStep):
    
    async def on_start(self):
        self.summary = "Waiting for Target Temp..."
        self.boil_kettle = self.get_kettle(self.props.get("Boil Kettle", None))
        self.boil_kettle.target_temp = float(self.props.get("Target Temp", 50))
        self.wort_pump = self.props.get("Wort Pump", None)
        self.alarm = self.props.get("Alarm", None)
        self.input = self.props.get("Input", None)
        self.input_actor = None if self.input is None else self.cbpi.actor.find_by_id(self.input)
        await self.push_update()

    async def on_stop(self):
        self.summary = ""
        await toggle_off(self, self.wort_pump)
        await toggle_off(self, self.alarm)
        await self.push_update()

    async def run(self):
        last_alarm = 10
        while self.running == True:
            
            if self.get_sensor_value(self.boil_kettle.sensor).get("value") > self.boil_kettle.target_temp:
                await toggle_on(self, self.wort_pump)
            
            if self.get_sensor_value(self.boil_kettle.sensor).get("value") <= self.boil_kettle.target_temp:
                await toggle_off(self, self.wort_pump)
                if last_alarm >= 10:
                    self.summary = "Target Temp reached."
                    await toggle_on(self, self.alarm)
                    self.cbpi.notify(self.name, 'Target Temp reached. Transfer wort to fermenter.', NotificationType.SUCCESS)
                    last_alarm = 0
                    await self.push_update()
                if checkActorOn(self.input_actor):
                    await self.next()
            last_alarm = last_alarm + 1                            
            await asyncio.sleep(1)
                
        return StepResult.DONE
    
# Utility functions

async def setAutoMode(cbpi, kettle, auto_state):
    try:
        if (kettle.instance is None or kettle.instance.state == False) and (auto_state is True):
            url = "http://127.0.0.1:" + str(cbpi.static_config.get('port',8000)) + "/kettle/" + str(kettle.id) + "/toggle"
            async with aiohttp.ClientSession() as session:
                async with session.post(url) as response:
                    return await response.text()            
        elif (kettle.instance.state == True) and (auto_state is False):
            await kettle.instance.stop()
    except Exception as e:
        logging.error("Failed to switch KettleLogic {} {}".format(kettle.id, e))

async def toggle_on(cbpi, actor):
    if actor is not None:
        #logging.info("turning on actor " + str(actor))
        await cbpi.actor_on(actor)
        
async def toggle_off(cbpi, actor):
    if actor is not None:
        #logging.info("turning off actor " + str(actor))
        await cbpi.actor_off(actor)

def checkActorOn(actor):
    return actor is not None and actor.instance is not None and actor.instance.state == True

def format_time(time):
    pattern_h = '{0:02d}:{1:02d}:{2:02d}'
    pattern_d = '{0:02d}D {1:02d}:{2:02d}:{3:02d}'
    seconds = time % 60
    minutes = math.floor(time / 60) % 60
    hours = math.floor(time / 3600) % 24
    days = math.floor(time / 86400)
    if days != 0:
        remaining_time = pattern_d.format(days, hours, minutes, seconds)
    else:
        remaining_time = pattern_h.format(hours, minutes, seconds)
    return remaining_time

class Timer(object):

    def __init__(self, timeout, on_done = None, on_update = None) -> None:
        super().__init__()
        self.timeout = timeout
        self._timeout = self.timeout
        self._task = None
        self._callback = on_done
        self._update = on_update
        self.start_time = None
        self.end_time = None
    
    def done(self, task):
        if self._callback is not None:
            asyncio.create_task(self._callback(self))

    async def _job(self):
        self.start_time = int(time.time())
        self.end_time = self.start_time + self._timeout
        self.count = self.end_time - self.start_time
        
        try:

            while self.count > 0:
                self.count = (self.end_time - int(time.time()))
                if self._update is not None:
                    await self._update(self, self.count)
                await asyncio.sleep(1)        
        except asyncio.CancelledError:
            end = int(time.time())
            duration = end - self.start_time
            self._timeout = self._timeout - duration

    async def add(self, seconds):
        self.end_time = self.end_time + seconds
        self._timeout = self._timeout + seconds

    def start(self):
        self._task = asyncio.create_task(self._job())
        self._task.add_done_callback(self.done)

    async def stop(self):
        if self._task and self._task.done() is False:
            self._task.cancel()
            await self._task

    def reset(self):
        if self.is_running is True:
            return
        self._timeout = self.timeout

    def is_running(self):
        return not self._task.done()

    def set_time(self, timeout):
        if self.is_running is True:
            return
        self._timeout = timeout

    def get_time(self):
        return self._timeout
        
def setup(cbpi):
    '''
    This method is called by the server during startup 
    Here you need to register your plugins at the server

    :param cbpi: the cbpi core 
    :return: 
    '''    
    
    cbpi.plugin.register("Laurelstein Notification Step", Laurelstein_NotificationStep)
    cbpi.plugin.register("Laurelstein Timer Step", Laurelstein_TimerStep)
    cbpi.plugin.register("Laurelstein Mash-In Step", Laurelstein_MashInStep)
    cbpi.plugin.register("Laurelstein Mash Step", Laurelstein_MashStep)
    cbpi.plugin.register("Laurelstein Mash-Out Step", Laurelstein_MashOutStep)
    cbpi.plugin.register("Laurelstein Sparge Step", Laurelstein_SpargeStep)
    cbpi.plugin.register("Laurelstein Sparge With Hardwired Floats Step", Laurelstein_SpargeWithHardwiredFloatsStep)
    cbpi.plugin.register("Laurelstein Boil Step", Laurelstein_BoilStep)
    cbpi.plugin.register("Laurelstein Cooldown Step", Laurelstein_CooldownStep)
    
    
    

    
