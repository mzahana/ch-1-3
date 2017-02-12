#!/usr/bin/env python


# Modules import
import rospy
import numpy as np
import tf
import datetime

from math import *
from std_msgs.msg import *
from sensor_msgs.msg import *
from geometry_msgs.msg import *
from mavros_msgs.msg import *
from mavros_msgs.srv import *

from autopilots.msg import StateMachine

import autopilotLib
import myLib
import autopilotParams

# TODO: move the gripper msg definition from autopilots pckg toi gripper pckg

#!!!!!!!!!!!!! Need to define a message type for the state machine !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
######################################################################################################
# structure of the StateMachine.msg
#	Headaer header
#	string	state
#	string	signal

#!!!!!!!!!!!!! Need to define a message type for the Gripper Feedback !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
########################################################################################################
# structure of the GripperFeedback.msg
#	Headaer header
#	bool[]	pushbutton_state	# holds the state of each pushbutton
#	bool	picked			# indicates an object is picked or not

#!!!!!!!!!!!!! Need to define a message type for the Gripper actuation !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#########################################################################################################
# structure of the GripperAction.msg
#	Headaer header
#	bool	command	# true: activate. false: deactivate
###### Helper Classes ######

###### quad_zone Class ######
class quad_zone(object):

    def __init__(self,ns,field_map):
	"""Creates a polygon.
        Keyword arguments:
        poly -- should be tuple of tuples contaning vertex in (x, y) form.
            e.g. [  [0,0],
                    [0,1],
                    [1,1],
                    [1,0],
                ]
        """
	if(ns=="/Quad1"):
		self.shape= [field_map[0],field_map[1],field_map[2],field_map[3],field_map[9],field_map[8],field_map[7]]
	elif(ns=="/Quad2"):
		self.shape= [field_map[6],field_map[5],field_map[4],field_map[3],field_map[9],field_map[10],field_map[11]]
	elif(ns=="/Quad3"):
        	self.shape= [field_map[0],field_map[7],field_map[11],field_map[6]]
	self.id=ns

    def is_inside(self, point):
        """Returns True if the point lies inside the polygon, False otherwise.
        Works on Ray Casting Method (https://en.wikipedia.org/wiki/Point_in_polygon)

        Keyword arguments:
        point -- a tuple representing the coordinates of point to be tested in (x ,y) form.
        """
        poly = self.shape
        n = len(self.shape)
        x, y = point
        inside = False

        p1x,p1y = self.shape[0]
        for i in range(n+1):
            p2x,p2y = self.shape[i % n]
            if y > min(p1y,p2y):
                if y <= max(p1y,p2y):
                    if x <= max(p1x,p2x):
                        if p1y != p2y:
                            xints = (y-p1y)*(p2x-p1x)/float(p2y-p1y)+p1x
                        if p1x == p2x or x <= xints:
                            inside = not inside
            p1x,p1y = p2x,p2y

        return inside
#### end of quad_zone Class ####
###### path tracker Class ######
class path_tracker():
	def __init__(self,areaBoundaries):
		self.index=0
		self.start=rospy.get_time()
		self.stop=rospy.get_time()
		self.elapsed=self.start-self.stop
		self.state="still"
		self.way_points_list=[]
		self.object_position=[]
#### end of path tracker Class ####

###### State Machine Class ######
# States: {Start, Idle, Takeoff, ObjectSearch, Picking, GoToDrop, WaitToDrop, Drop, GoHome, Land, Hover}
# Possible Signals for each state:
#	Start:        {'Waiting', 'Ready'}
#	Takeoff:      {'Done', 'Running', 'Interrupted'}
#	ObjectSearch: {'Done', 'Running', 'Interrupted'}
#	Picking:      {'Done', 'Running', 'Failed', 'Interrupted'}
#	GoToDrop:     {'Done', 'Running', 'Interrupted'}
#   	WaitToDrop:   {'Done', 'Running', 'Interrupted'}
#	Dropping:     {'Done', 'Running', 'Interrupted'}
#	GoHome:       {'Done', 'Running', 'Interrupted'}
#	Land:         {'Done', 'Running', 'Interrupted'}
#	Hover:        {'Done', 'Running'}   # All state should go to Hover when Interrupted
class StateMachineC( object ):
	def __init__(self, ns, field_map):
		autopilotParams.setParams(ns)
		# ns: namespace [REQUIRED]. Always append it to subscribed/published topics
		self.namespace		= ns
		# The list of the different field refence points (to be provided)
		self.areaBoundaries     = field_map
		#Parameter that caracterize the camera precision and field of view
		self.cameraView		=6
		# object that is used for the tracking of points to be visited
		self.way_points_tracker=path_tracker(self.areaBoundaries)
		self.way_points_tracker.way_points_list=self.path()
		self.quad_op_area =quad_zone(ns,field_map)
		# Initially/finally, do nothing.
		self.current_state	= 'Idle'
		# used to decide on transitions to other states
		self.current_signal	= None
		# last state to resume after external interruption
        	self.resume_state   = 'Hover'
		# to indicate error staus in state machine (for debug)
		self.erro_signal	= False
		# Error description (for debug)
		self.error_str		= 'No error'
		# Turn debug mode on/off
		self.DEBUG		= False
		# a flag to start the state machine, if true
		self.START_SIGNAL	= False
		if(self.namespace=="/Quad1"):
			self.ns_other_1="/Quad2"
			self.ns_other_2="/Quad3"
		elif(self.namespace=="/Quad2"):
			self.ns_other_1="/Quad1"
			self.ns_other_2="/Quad3"
		elif(self.namespace=="/Quad3"):
			self.ns_other_1="/Quad1"
			self.ns_other_2="/Quad2"
		# internal state-related fields
		self.current_lat	= 0.0
		self.current_lon	= 0.0
		self.other_1_current_lat= 0.0
		self.other_2_current_lat= 0.0
		self.other_1_current_lon= 0.0
		self.other_2_current_lon= 0.0
		self.other_1_state	= 0.0
		self.other_2_state	= 0.0
		self.target_lat		= 1.1
		self.target_lon		= 1.1

		# takeoff altitude [m] to be set by external controller
		self.TKOFFALT		= 2.0
		# Takeoff velocity [m/s]
		self.TAKEOFF_V		= 1.0

		# Landing velocity [m/s]
		self.LANDING_V		= 0.5

		# Lat/Lon of pre-drop location: different for each vehicle
		self.PRE_DROP_COORDS	= np.array([23.1, 12.1])
		self.DROP_COORDS	= np.array([23.3, 12.5])

		# Altitude at ground level
		self.ZGROUND		= 0.0
		self.home		= autopilotLib.xyzVar()
		# Altitude at which we pick object [m]
		self.PICK_ALT		= 0.6
		# camera offset from ground [m]
		self.CAMOFFSET		= 0.0
		# relative pos error where descend is allowed [m]
		self.ENVELOPE_XY_POS	= 0.2
		# relative vel error where descend is allowed [m/s]
		self.ENVELOPE_XY_V	= 0.1

		# Instantiate a setpoint topic structure
		self.setp		= PositionTarget()
		# use velocity setpoints
		self.setp.type_mask	= int('010111000111', 2)

		# Instantiate altitude controller object (from autopilot library)
		self.altK 		= autopilotLib.kAltVel(ns)

		# Instantiate body controller object (from autopilot library)
		self.bodK 		= autopilotLib.kBodVel(ns)

		# Instantiate a tracker (blue)
		# xyz location of object w.r.t quad [m]. z only used to indicate if object is tracked or not
		self.blue_target 		= autopilotLib.xyzVar()
		rospy.Subscriber(ns+'/getColors/blue/xyMeters', Point32, self.blue_target.cbXYZ)

		# Instantiate a tracker (green)
		self.green_target 		= autopilotLib.xyzVar()
		rospy.Subscriber(ns+'/getColors/green/xyMeters', Point32, self.green_target.cbXYZ)

		# other colors.......?

		# subscribe to BGR object
		self.bgr_target 		= autopilotLib.xyzVar()
		rospy.Subscriber(ns+'/getColors/bgr/xyMeters', Point32, self.bgr_target.cbXYZ)

		# Establish a rate
		self.fbRate 		= rospy.get_param(ns+'/autopilot/fbRate')
		self.rate 		= rospy.Rate(self.fbRate)

		# Subscriber to mavros GPS topic
		rospy.Subscriber(ns+'/mavros/global_position/global', NavSatFix, self.gps_cb)
		# Subscriber to mavros others GPS topic
		rospy.Subscriber(self.ns_other_1+'/mavros/global_position/global', NavSatFix, self.gps_other_1_cb)
		rospy.Subscriber(self.ns_other_2+'/mavros/global_position/global', NavSatFix, self.gps_other_2_cb)

		# Subscriber to mavros others states topic
		rospy.Subscriber(self.ns_other_1+'/state_machine/state', StateMachine, self.state_other_1_cb)
		rospy.Subscriber(self.ns_other_2+'/state_machine/state', StateMachine, self.state_other_2_cb)


		# setpoint publisher (velocity to Pixhawk)
		self.command 		= rospy.Publisher(ns+'/mavros/setpoint_raw/local', PositionTarget, queue_size=10)

		# State Machine topic & its publisher
		self.state_topic	= StateMachine()
		self.state_pub		= rospy.Publisher(ns+'/state_machine/state', StateMachine, queue_size=10)

		# Gripper feedback topic
		# False: not picked, True: picked
		self.gripperIsPicked	= False
		rospy.Subscriber(ns+'/gripper_status', Bool, self.gripper_cb)

		# Gripper command topic
		# .data=True: activate magnets, .data=False: deactivate
		self.gripper_action	= Bool()
		self.gripper_pub	= rospy.Publisher(ns+'/gripper_command', Bool, queue_size=10)
		# set state Interruption as ROS paramter
		# if >0 : interrupt state
		rospy.set_param(ns+'/state_machine/interruption', 0.0)

		# set Resume signal as ROS parameter
		# >0: resume
		rospy.set_param(ns+'/state_machine/resume', 0.0)

#----------------------------------------------------------------------------------------------------------------------------------------#
#                                                   (States implementation)                                                              #

	# State: start
	def execute_start(self):
		self.current_state='Start'
		self.current_signal='Waiting'

		self.debug()

		while not self.START_SIGNAL and not rospy.is_shutdown():
			# do nothing, until we receive the start signal

			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

		self.current_signal = 'Ready'
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)

		self.debug()

		return


	# State: Takeoff
	def execute_takeoff(self):
		self.current_state = 'Takeoff'
		self.current_signal = 'Running'

		self.debug()
		# cycle for some time to register local poisiton
		c=0
		while c<2:
		    self.rate.sleep()
		    c = c + 1
		# get ground level
		self.ZGROUND = self.altK.z
		# set the controllers setpoints
		#self.altK.zSp = self.ZGROUND + rospy.get_param(self.namespace+'/autopilot/altStep')
		self.altK.zSp = self.ZGROUND + self.TKOFFALT
		self.home.x = self.bodK.x
		self.home.y = self.bodK.y

		# save current lateral vMax
		saved_vmax = rospy.get_param(self.namespace + '/kBodVel/vMax')
		# lower lateral vMax
		rospy.set_param(self.namespace + '/kBodVel/vMax', 0.5)

		# takeoff
		while abs(self.altK.zSp - self.altK.z) > 0.2 and not rospy.is_shutdown():
			self.setp.velocity.z = self.altK.controller()
			(self.bodK.xSp, self.bodK.ySp) = autopilotLib.wayHome(self.bodK, self.home)
			(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
			self.rate.sleep()
			self.setp.header.stamp = rospy.Time.now()
			self.command.publish(self.setp)
			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

			print 'State/Set/Alt/Gnd:', self.current_state, self.altK.zSp, self.altK.z, self.ZGROUND

			# check for interruption
			if rospy.get_param(self.namespace+'/state_machine/interruption')>0.0:
			    self.current_signal='Interrupted'
			    # clear the interruption
			    rospy.set_param(self.namespace+'/state_machine/interruption',0.0)
			    break

		# reset lateral vMax
		rospy.set_param(self.namespace + '/kBodVel/vMax', saved_vmax)

		# Done with takoeff, send signal
		if self.current_signal != 'Interrupted':
		    self.current_signal = 'Done'
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)
		# update setpoint topic
		self.setp.velocity.z = self.altK.controller()
		(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
		self.rate.sleep()
		# publish setpoints
		self.setp.header.stamp = rospy.Time.now()
		self.command.publish(self.setp)

		self.debug()

		return

	######### Done with Takeof State #########

	# State: ObjectSearch
	def execute_objectSearch(self):
		print("I am in object search...")
		self.current_state = 'ObjectSearch'
		self.current_signal= 'Running'

		self.debug()

		objectFound = False

		# execute some trajectory, e.g. circle
		# keep checking vision feedback
		# once an object is found, exit current state
		while  not objectFound and not rospy.is_shutdown():
			# TODO executing search trajectory
			if(self.way_points_tracker.object_position!=[]):
				self.target_lat=self.way_points_tracker.object_position[0]
				self.target_lon=self.way_points_tracker.object_position[1]
				if((self.way_points_tracker.state == "still") and (sqrt(pow(self.home.x-self.bodK.x,2)+pow(self.home.y-self.bodK.y,2))<1)):
					self.way_points_tracker.start = rospy.get_time()
					self.way_points_tracker.state="checking"
					print("cheking")
				if(self.way_points_tracker.state == "checking"):
					self.way_points_tracker.stop = rospy.get_time()
					self.way_points_tracker.elapsed = self.way_points_tracker.stop - self.way_points_tracker.start
					if (self.way_points_tracker.elapsed>2):
					    self.way_points_tracker.state="reached"
					    print("reached")
				if(self.way_points_tracker.state == "reached"):
					self.target_lat=self.way_points_tracker.way_points_list[self.way_points_tracker.index][0]
					self.target_lon=self.way_points_tracker.way_points_list[self.way_points_tracker.index][1]
					self.way_points_tracker.state="still"
					print("Going to :")
					print(self.way_points_tracker.way_points_list[self.way_points_tracker.index][0])
					print(self.way_points_tracker.way_points_list[self.way_points_tracker.index][1])
					self.way_points_tracker.object_position=[]
					self.way_points_tracker.start=rospy.get_time()
					self.way_points_tracker.stop = self.way_points_tracker.start
					self.way_points_tracker.elapsed = self.way_points_tracker.stop - self.way_points_tracker.start

				(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.target_lat, self.target_lon)
				self.home.x = self.bodK.x + dx_enu
				self.home.y = self.bodK.y + dy_enu
			elif(self.way_points_tracker.index < len(self.way_points_tracker.way_points_list)):
				self.target_lat=self.way_points_tracker.way_points_list[self.way_points_tracker.index][0]
				self.target_lon=self.way_points_tracker.way_points_list[self.way_points_tracker.index][1]
				if((self.way_points_tracker.state == "still") and (sqrt(pow(self.home.x-self.bodK.x,2)+pow(self.home.y-self.bodK.y,2))<1)):
					self.way_points_tracker.start = rospy.get_time()
					self.way_points_tracker.state="checking"
					print("cheking")
				if(self.way_points_tracker.state == "checking"):
					self.way_points_tracker.stop = rospy.get_time()
					self.way_points_tracker.elapsed = self.way_points_tracker.stop - self.way_points_tracker.start
					if (self.way_points_tracker.elapsed>2):
					    self.way_points_tracker.state="reached"
					    print("reached")
				if(self.way_points_tracker.state == "reached"):
					self.way_points_tracker.index=self.way_points_tracker.index+1
					print(self.way_points_tracker.index)
					self.target_lat=self.way_points_tracker.way_points_list[self.way_points_tracker.index][0]
					self.target_lon=self.way_points_tracker.way_points_list[self.way_points_tracker.index][1]
					self.way_points_tracker.state="still"
					print("Going to :")
					print(self.way_points_tracker.way_points_list[self.way_points_tracker.index][0])
					print(self.way_points_tracker.way_points_list[self.way_points_tracker.index][1])
					self.way_points_tracker.start=rospy.get_time()
					self.way_points_tracker.stop = self.way_points_tracker.start
					self.way_points_tracker.elapsed = self.way_points_tracker.stop - self.way_points_tracker.start

				(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.target_lat, self.target_lon)
				self.home.x = self.bodK.x + dx_enu
				self.home.y = self.bodK.y + dy_enu
			else:
				self.way_points_tracker.index=0

			# check for objects
			#objectFound, _ = self.monitorObjects()
			objectFound, _ = self.monitorSingleObject()


			# publish control commands
			(self.bodK.xSp, self.bodK.ySp) = autopilotLib.wayHome(self.bodK, self.home)
			self.setp.velocity.z = self.altK.controller()
			(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
			self.rate.sleep()
			# publish setpoint to pixhawk
			self.setp.header.stamp = rospy.Time.now()
			self.command.publish(self.setp)
			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

			# check for interruption
			if rospy.get_param(self.namespace+'/state_machine/interruption')>0.0:
			    self.current_signal='Interrupted'
			    # clear the interruption
			    rospy.set_param(self.namespace+'/state_machine/interruption',0.0)
			    break

		# Done with ObjectSearch, send signal
		if self.current_signal != 'Interrupted':
		    self.current_signal = 'Done'
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)
		# update setpoint topic
		self.setp.velocity.z = self.altK.controller()
		(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
		self.rate.sleep()
		# publish setpoints
		self.setp.header.stamp = rospy.Time.now()
		self.command.publish(self.setp)

		self.debug()

		return
	####### Done with ObjectSearch State #####

	# State: Picking
	def execute_picking(self):
		self.current_state = 'Picking'
		self.current_signal = 'Running'

		self.debug()

		# 1) track closest object, keep constant altitude
		# 2) once inside a pick envelope, descend, while tracking
		# 3) once below some LOW Alt, check pick state, keep tracking
		# 4) if low and picked, fly up, exit Picking state
		# 5) if low, and not picked, and timeout(maybe not?), fly up to search/track again goto(1)
		# !!!!!!!!!!!!!! Always make sure that you are in your assigned area!!!!!!!!!

		# temp variables
		objectFound = False
		xy=[0,0]
		picked = False

		# set altitude
		self.altK.zSp= self.ZGROUND + rospy.get_param(self.namespace+'/autopilot/altStep')

		# get current max lateral velocity
		saved_vmax = rospy.get_param(self.namespace + '/kBodVel/vMax')

		# TODO: Activate gripper (write the ROS node for the gripper feedback/command)
		self.gripper_action.data = True
		self.gripper_pub.publish(self.gripper_action)

		while  self.current_signal != 'Failed' and not picked and not rospy.is_shutdown():
			# monitor objects
			#objectFound, xy = self.monitorObjects()
			objectFound, xy = self.monitorSingleObject()

			# found an object
			if objectFound and not self.gripperIsPicked:
				altCorrect = (self.altK.z - self.ZGROUND + self.CAMOFFSET)/rospy.get_param(self.namespace+'/pix2m/altCal')
				self.bodK.xSp = xy[0]*altCorrect
				self.bodK.ySp = xy[1]*altCorrect
				# for debug
				print 'XY setpoints: ', self.bodK.xSp, '/', self.bodK.ySp
				# this is a good observation, store it
				# store most recent successful target
				self.home.x = self.bodK.x
				self.home.y = self.bodK.y
				if self.inside_envelope(xy):
					# descend if inside envelope
					self.altK.zSp = max(self.altK.z - 0.1*abs(self.altK.z), self.ZGROUND+self.PICK_ALT)
			# object not found
			elif not objectFound and not self.gripperIsPicked:
				# if at max ALT (stil did not find objects), exit state with signal='Failed', to search again
				if (self.altK.z >= self.ZGROUND+rospy.get_param(self.namespace+'/autopilot/altStep')):
					self.current_signal = 'Failed'

				# set last location where object was seen
				(self.bodK.xSp, self.bodK.ySp) = autopilotLib.wayHome(self.bodK,self.home)
				# increase altitude gradually, until an object is seen again
				self.altK.zSp = min(self.altK.z + 0.1*abs(self.altK.z), self.ZGROUND+rospy.get_param(self.namespace+'/autopilot/altStep'))

			# check if object is picked, fly up 1 meter
			if (self.gripperIsPicked):
				self.altK.zSp = self.ZGROUND + 1.0
			# check if still picked up at 1 meter, then claim object is picked
			if (self.gripperIsPicked and self.altK.z > (self.ZGROUND+1.0) ):
				picked = True	# set True to exit the while loop
				self.altK.zSp = self.ZGROUND + rospy.get_param(self.namespace+'/autopilot/altStep')

			# make sure to lower max lateral velocity if close to ground
			# this is to avoid flipping at high velocity close to ground
			if self.altK.z < (self.ZGROUND + 0.5) :
				# if less the 0.5 meter from ground, set vMax = 0.5 [m/s]
				rospy.set_param(self.namespace + '/kBodVel/vMax', 0.5)
			else:
				rospy.set_param(self.namespace + '/kBodVel/vMax', saved_vmax)

			#TODO: Fix me, keep sending activation signal when at low ALT
			if self.altK.z < (self.ZGROUND + 0.5):
				self.gripper_action.data = True
				self.gripper_pub.publish(self.gripper_action)

			self.setp.velocity.z = self.altK.controller()
			(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
			self.rate.sleep()
			# publish setpoint to pixhawk
			self.setp.header.stamp = rospy.Time.now()
			self.command.publish(self.setp)
			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

			# check for interruption
			if rospy.get_param(self.namespace+'/state_machine/interruption')>0.0:
			    self.current_signal='Interrupted'
			    # clear the interruption
			    rospy.set_param(self.namespace+'/state_machine/interruption',0.0)
			    break

		# Done with Picking state, send signal
		# Make sure the signal is not 'Failed' and not interrupted, before declaring 'Done' signal
		if (self.current_signal != 'Failed' and self.current_signal != 'Interrupted'):
			self.current_signal = 'Done'
		#save the picked object position
		self.way_points_tracker.object_position=[self.current_lat,self.current_lon]
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)
		# update setpoint topic
		self.setp.velocity.z = self.altK.controller()
		(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
		self.rate.sleep()
		# publish setpoints
		self.setp.header.stamp = rospy.Time.now()
		self.command.publish(self.setp)

		self.debug()

		return




	# State: GoToDrop
	def execute_gotodrop(self):
		self.current_state = 'GoToDrop'
		self.current_signal = 'Running'

		self.debug()

		# temp variables
		arrived = False

		while not arrived and not rospy.is_shutdown():
			# TODO: convert the pre-drop location to local ENU (MAVROS expects ENU not NED)
			# (x_enu, y_enu) = self.get_enu_from_gps(self.DROP_COORDS)
			# self.home.x = x_enu
			# self.home.y = y_enu

			# TODO: implement trajectory to go to PRE_DROP zone
			if(self.namespace=="/Quad1"):
				(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.areaBoundaries[8][0], self.areaBoundaries[8][1])
				self.home.x = self.bodK.x + dx_enu
				self.home.y = self.bodK.y + dy_enu
			elif(self.namespace=="/Quad2"):
				(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.areaBoundaries[10][0], self.areaBoundaries[10][1])
				self.home.x = self.bodK.x + dx_enu
				self.home.y = self.bodK.y + dy_enu
			elif(self.namespace=="/Quad3"):
				(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.areaBoundaries[12][0], self.areaBoundaries[12][1])
				self.home.x = self.bodK.x + dx_enu
				self.home.y = self.bodK.y + dy_enu
			(self.bodK.xSp, self.bodK.ySp) = autopilotLib.wayHome(self.bodK, self.home)
			self.altK.zSp = self.ZGROUND + rospy.get_param(self.namespace+'/autopilot/altStep')

			# check if arrived
			if np.sqrt(self.bodK.xSp**2 + self.bodK.ySp*2) < 0.2:
				arrived = True	# set to True to leave the while loop

			# compute setpoints
			self.setp.velocity.z = self.altK.controller()
			(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
			self.rate.sleep()
			# publish setpoints
			self.setp.header.stamp = rospy.Time.now()
			self.command.publish(self.setp)
			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

			# check for interruption
			if rospy.get_param(self.namespace+'/state_machine/interruption')>0.0:
			    self.current_signal='Interrupted'
			    # clear the interruption
			    rospy.set_param(self.namespace+'/state_machine/interruption',0.0)
			    break

		# Done with GoToDrop, send signal
		if self.current_signal != 'Interrupted':
		          self.current_signal = 'Done'
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)
		# update setpoint topic
		self.setp.velocity.z = self.altK.controller()
		(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
		self.rate.sleep()
		self.setp.header.stamp = rospy.Time.now()
		# publish setpoints
		self.command.publish(self.setp)

		self.debug()

		return

	# State: WaitToDrop
	def execute_waittodrop(self):
		self.current_state='WaitToDrop'
		self.current_signal='Running'

		self.debug()

		# while loop
		while not rospy.is_shutdown():
		# TODO: Implement coordinated dropping below
			(dy_enu_other_1, dx_enu_other_1) = self.LLA_local_deltaxy(self.areaBoundaries[13][0], self.areaBoundaries[13][1], self.other_1_current_lat, self.other_1_current_lon)
			distance_other_1=sqrt(pow(dy_enu_other_1,2)+pow(dx_enu_other_1,2))
			(dy_enu_other_2, dx_enu_other_2) = self.LLA_local_deltaxy(self.areaBoundaries[13][0], self.areaBoundaries[13][1], self.other_2_current_lat, self.other_2_current_lon)
			distance_other_2=sqrt(pow(dy_enu_other_2,2)+pow(dx_enu_other_2,2))

			if(self.namespace=="/Quad1"):
				if (not(self.other_1_state=="Dropping") and  not(self.other_2_state=="Dropping") and (distance_other_1>6) and (distance_other_2>6)):
					self.current_signal = 'Done'
					break


			elif(self.namespace=="/Quad2"):
				if (not(self.other_1_state=="WaitToDrop") and not(self.other_1_state=="Dropping") and not(self.other_2_state=="Dropping") and (distance_other_1>6) and (distance_other_2>6)):
					self.current_signal = 'Done'
					break

			elif(self.namespace=="/Quad3"):
				if (not(self.other_1_state=="WaitToDrop") and not(self.other_1_state=="Dropping") and not(self.other_2_state=="WaitToDrop") and not(self.other_2_state=="Dropping") and (distance_other_1>6) and (distance_other_2>6)):
					self.current_signal = 'Done'
					break

			# get setpoint
			if(self.namespace=="/Quad1"):
				(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.areaBoundaries[8][0], self.areaBoundaries[8][1])
				self.home.x = self.bodK.x + dx_enu
				self.home.y = self.bodK.y + dy_enu
			elif(self.namespace=="/Quad2"):
				(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.areaBoundaries[10][0], self.areaBoundaries[10][1])
				self.home.x = self.bodK.x + dx_enu
				self.home.y = self.bodK.y + dy_enu
			elif(self.namespace=="/Quad3"):
				(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.areaBoundaries[12][0], self.areaBoundaries[12][1])
				self.home.x = self.bodK.x + dx_enu
				self.home.y = self.bodK.y + dy_enu

			(self.bodK.xSp, self.bodK.ySp) = autopilotLib.wayHome(self.bodK, self.home)
			self.altK.zSp = self.ZGROUND + rospy.get_param(self.namespace+'/autopilot/altStep')

			# update setpoint topic
			self.setp.velocity.z = self.altK.controller()
			(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
			self.rate.sleep()
			# publish setpoints
			self.setp.header.stamp = rospy.Time.now()
			self.command.publish(self.setp)
			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

			# check for interruption
			if rospy.get_param(self.namespace+'/state_machine/interruption')>0.0:
			    self.current_signal='Interrupted'
			    # clear the interruption
			    rospy.set_param(self.namespace+'/state_machine/interruption',0.0)
			    break

		# Done with WaitToDrop, send signal
		if self.current_signal != 'Interrupted':
		          self.current_signal = 'Done'
		# update setpoint topic
		self.setp.velocity.z = self.altK.controller()
		(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
		self.rate.sleep()
		# publish setpoints
		self.setp.header.stamp = rospy.Time.now()
		self.command.publish(self.setp)
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)

		self.debug()

		return

	# State: Drop
	def execute_drop(self):
		self.current_state='Drop'
		self.current_signal='Running'

		self.debug()

		# TODO: look for dropbox (vision-based)
		# once centerd, go to drop alt
		# deactivate magnets, and keep checking gripper feedback!
		# while loop
		dropped = False
		while not dropped and not rospy.is_shutdown():

			# compute direction to drop waypoint
			drop_waypoint_index=13
			(dy_enu, dx_enu) = self.LLA_local_deltaxy(self.current_lat, self.current_lon, self.areaBoundaries[drop_waypoint_index][0], self.areaBoundaries[drop_waypoint_index][1])
			self.home.x = self.bodK.x + dx_enu
			self.home.y = self.bodK.y + dy_enu
			# TODO: switch to vision-based guidance once available
			# TODO: boxIsFound, xy = self.findBox()

			(self.bodK.xSp, self.bodK.ySp) = autopilotLib.wayHome(self.bodK, self.home)
			self.altK.zSp = self.ZGROUND + rospy.get_param(self.namespace+'/autopilot/altStep')

			# try to drop if arrived
			distance = sqrt(self.bodK.xSp**2 + self.bodK.ySp**2)
			if distance < 0.2:
				# deactivate gripper
				self.gripper_action.data = False
				self.gripper_pub.publish(self.gripper_action)

			#TODO break once drop is confirmed
			distance = sqrt(self.bodK.xSp**2 + self.bodK.ySp**2)
			if distance < 0.2 and not self.gripperIsPicked:
				dropped = True

			# update setpoint topic
			self.setp.velocity.z = self.altK.controller()
			(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
			self.rate.sleep()
			# publish setpoints
			self.setp.header.stamp = rospy.Time.now()
			self.command.publish(self.setp)
			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

			# check for interruption
			if rospy.get_param(self.namespace+'/state_machine/interruption')>0.0:
				self.current_signal='Interrupted'
				# clear the interruption
				rospy.set_param(self.namespace+'/state_machine/interruption',0.0)
				break

		# Done with Drop, send signal
		if self.current_signal != 'Interrupted':
			self.current_signal = 'Done'
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)
		# update setpoint topic
		self.altK.zSp = self.ZGROUND + rospy.get_param(self.namespace+'/autopilot/altStep')
		self.setp.velocity.z = self.altK.controller()
		(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
		self.rate.sleep()
		# publish setpoints
		self.setp.header.stamp = rospy.Time.now()
		self.command.publish(self.setp)

		self.debug()

		return

	# State: GoHome
	def execute_gohome(self):
		self.current_state='GoHome'
		self.current_signal='Running'

		self.debug()

		# go to the Home XY coordinates. Landing is handled by landing state, see below.

		# Done with GoHome state, send signal
		self.current_signal = 'Done'
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)

		self.debug()

		return

	# State: Land
	def execute_land(self):
		self.current_state='Land'
		self.current_signal='Running'

		self.debug()

		# landed flag
		landed = False

		# send land command to Pixhawk, or execute landing routine using the velocity controller

		# get current lateral vMax
		current_vmax = rospy.get_param(self.namespace + '/kBodVel/vMax')

		# lower lateral vMax params
		rospy.set_param(self.namespace + '/kBodVel/vMax', 0.5)

		# get current lateral position
		# cycle to register local position
		c=0
		while c<2:
			self.rate.sleep()
			c = c + 1
		self.altK.zSp = self.altK.z
		self.home.x = self.bodK.x
		self.home.y = self.bodK.y

		# counter for valid landed readings
		c_landed=0
		# number of valid landed readings before we declared LANDED
		VALID_LANDED_C = 10
		# velocity accumulator for valid landed velocity
		v_landed_hist = 0.0
		# average of history
		v_avg = 0.0
		# moving average window, before reset
		AVG_WINDOW = 20

		# while loop
		while not landed and not rospy.is_shutdown():
			# update setpoint topic
			self.setp.velocity.z = -0.1*self.LANDING_V # [-1 for going down]
			(self.bodK.xSp, self.bodK.ySp) = autopilotLib.wayHome(self.bodK, self.home)
			(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()

			# landed conditions
			# possible landing situation
			current_vz = abs(altK.vz)
			"""
			if current_z < 0.1:
				# increase counter
				c_landed = c_landed + 1
			else:
				c_landed = 0

			if c_landed > VALID_LANDED_C:
				# landing is Done
				landed = True
			"""

			# if inside the current average window, do averaging
			if c_landed < AVG_WINDOW:
				v_avg = (current_vz + c_landed*v_avg) / (c_landed+1)
				c_landed = c_landed + 1
			else: # otherwise, reset
				# check if the average velocity is below landing threshold. If yes, claim landing
				if v_avg < 0.1:
					landed = True
				c_landed = 0
				v_avg = 0.0

			self.rate.sleep()
			# publish setpoints
			self.setp.header.stamp = rospy.Time.now()
			self.command.publish(self.setp)
			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

		# reset original lateral vMax
		rospy.set_param(self.namespace + '/kBodVel/vMax', current_vmax)

		# Done with Land state, send signal
		self.current_signal = 'Done'
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)

		self.debug()

		return
	######## End of Land state ############################

	# State: Hover
	def execute_hover(self):
		self.current_state='Hover'
		self.current_signal='Running'

		self.debug()

		# cycle to register local position
		c=0
		while c<2:
			self.rate.sleep()
			c = c + 1
		self.altK.zSp = self.altK.z
		self.home.x = self.bodK.x
		self.home.y = self.bodK.y

		# reset Resume parameter
		rospy.set_param(self.namespace+'/state_machine/resume', 0.0)

		# while loop
		while not rospy.is_shutdown():
			# update setpoint topic
			self.setp.velocity.z = self.altK.controller()
			(self.bodK.xSp, self.bodK.ySp) = autopilotLib.wayHome(self.bodK, self.home)
			(self.setp.velocity.x, self.setp.velocity.y, self.setp.yaw_rate) = self.bodK.controller()
			self.rate.sleep()
			# publish setpoints
			self.setp.header.stamp = rospy.Time.now()
			self.command.publish(self.setp)
			# publish state topic
			self.state_topic.state = self.current_state
			self.state_topic.signal = self.current_signal
			self.state_pub.publish(self.state_topic)

			# check for resume command
			if rospy.get_param(self.namespace+'/state_machine/resume') > 0.0:
				# clear resume parameter
				rospy.set_param(self.namespace+'/state_machine/resume', 0.0)
				break

		# reset inturrupt state
		rospy.set_param(self.namespace+'/state_machine/interruption',0.0)

		# Done with Hover state, send signal
		self.current_signal = 'Done'
		# publish state topic
		self.state_topic.state = self.current_state
		self.state_topic.signal = self.current_signal
		self.state_pub.publish(self.state_topic)

		self.debug()
		return

#                                          (End of States Implementation)                                                                #
#----------------------------------------------------------------------------------------------------------------------------------------#

#----------------------------------------------------------------------------------------------------------------------------------------#
#                                          (State Transition Function)	                                                                 #
	def update_state(self):

		# States: {Idle, Takeoff, ObjectSearch, Picking, GoToDrop, WaitToDrop, Dropping, GoHome, Land}
		# Possible Signals for each state:
		#	Takeoff:	{'Done', 'Running', 'Interrupted'}
		#	ObjectSearch:	{'Done', 'Running', 'Interrupted'}
		#	Picking:	{'Done', 'Running', 'Failed', 'Interrupted'}
		#	GoToDrop:	{'Done', 'Running', 'Interrupted'}
		#	WaitToDrop:	{'Done', 'Running', 'Interrupted'}
		#	Drop:		{'Done', 'Running', 'Interrupted'}
		#	GoHome:		{'Done', 'Running', 'Interrupted'}
		#	Land:		{'Done', 'Running', 'Interrupted'}
		#	Hover:		{'Done', 'Running'}    # state should go to Hover when interrupted

		# manage the transition between states
		state = self.current_state
		signal = self.current_signal

		if (state == 'Start' and signal != 'Ready' and self.START_SIGNAL):	# initial signal
			self.execute_start()
		elif (state == 'Start' and signal == 'Ready'):
			self.execute_takeoff()

		elif (state == 'Takeoff'):
			if signal == 'Done':
				self.execute_objectSearch()
			elif signal == 'Interrupted':
				self.resume_state='Takeoff'
				self.execute_hover()
			elif signal == 'Resume':
				self.execute_takeoff()

		elif (state == 'ObjectSearch'):
			if signal == 'Done':
				self.execute_picking()
			elif signal == 'Interrupted':
				self.resume_state='ObjectSearch'
				self.execute_hover()
			elif signal == 'Resume':
				self.execute_objectSearch()

		elif (state == 'Picking'):
			if signal == 'Interrupted':
				self.resume_state='Picking'
				self.execute_hover()
			elif (signal == 'Failed'):
				self.execute_objectSearch()
			elif (signal == 'Done'):
				self.execute_gotodrop()
			elif signal == 'Resume':
				self.execute_picking()
		elif (state == 'GoToDrop'):
			if signal == 'Interrupted':
				self.resume_state = 'GoToDrop'
				self.execute_hover()
			elif signal == 'Done':
				self.execute_waittodrop()
			elif signal == 'Resume':
				self.execute_gotodrop()

		elif (state == 'WaitToDrop'):
			if signal == 'Interrupted':
				self.resume_state = 'WaitToDrop'
				self.execute_hover()
			elif signal == 'Done':
				self.execute_drop()
			elif signal == 'Resume':
				self.execute_waittodrop()

		elif (state == 'Drop'):
			if signal == 'Interrupted':
				self.resume_state = 'Drop'
				self.execute_hover()
			elif signal == 'Done':
				self.execute_objectSearch()
			elif signal == 'Resume':
				self.execute_drop()

		elif (state == 'Hover'):
			if signal == 'Done':
				self.current_state = self.resume_state
				self.current_signal = 'Resume'
#                                          (End of transition function)                                                                  #
#----------------------------------------------------------------------------------------------------------------------------------------#

#----------------------------------------------------------------------------------------------------------------------------------------#
#                                               (helper functions)                                                                       #

	# check if a colored object is found
	# returns a tuple: (bool objectFound, xy_coord of closest object)
	def monitorObjects(self):
		# define distance to each color object
		d_to_blue=np.inf
		d_to_green=np.inf

		# radius list: of detected objects
		r_list=[]
		# list of x/y coords of found objects
		xy_list=[]

		# flag if object is found
		objectFound = False

		# update the distance if (blue) is found
		if self.blue_target.z > 0 :
			d_to_blue = np.sqrt(self.blue_target.x**2 + self.blue_target.y**2)
			r_list.append(d_to_blue)
			xy_list.append([self.blue_target.x, self.blue_target.y,d_to_blue])

		# update the distance if (green) is found
		if self.green_target.z > 0 :
			d_to_green = np.sqrt(self.green_target.x**2 + self.green_target.y**2)
			r_list.append(d_to_green)
			xy_list.append([self.green_target.x, self.green_target.y,d_to_green])

		# other colors..........?

		# find the closest object that is inside the area of operation
			# Finally return
		###################TODO modified and need to be checked #########################################
		if (len(xy_list)>0):
			xy_list_sorted=[]
			for i in range(0,len(xy_list)):
				xy_list_sorted.append([xy_list[i][0],xy_list[i][1],r_list[i]])
			xy_list_sorted=sorted(xy_list, key=self.getThirdElemt)
			for i in range(0,len(xy_list_sorted)):
				bodyRot = self.bodK.yaw - pi/2.0
				x_enu =  xy_list_sorted[i][1]*cos(bodyRot) - xy_list_sorted[i][0]*sin(bodyRot)
				y_enu = xy_list_sorted[i][1]*sin(bodyRot) + xy_list_sorted[i][0]*cos(bodyRot)
				dx_enu = x_enu
				dy_enu = y_enu
				##x and y switched because this function operates in NED frame
				[lat_object,lon_object]=self.local_deltaxy_LLA(self.current_lat, self.current_lon,  dy_enu,  dx_enu)
				if( self.quad_op_area.is_inside([lat_object,lon_object]) ):
					objectFound=True
					return (objectFound, [xy_list_sorted[i][0],xy_list_sorted[i][1]])
				else:
					print("Object seen but neglected")
					objectFound = False
					return (objectFound, [])
		else:
			objectFound=False
			return (objectFound, [])
		############################################################################

	########## End of Monitoring Colored Objects #######################

	# check if a single object is found
	# returns a tuple: (bool objectFound, xy_coord of object with biggest contour)
	def monitorSingleObject(self):
		# define distance to each color object
		d_to_object=np.inf

		# radius list: of detected objects
		r_list=[]
		# list of x/y coords of found objects
		xy_list=[]

		# flag if object is found
		objectFound = False

		# update the distance if (blue) is found
		if self.bgr_target.z > 0 :
			d_to_object = np.sqrt(self.bgr_target.x**2 + self.bgr_target.y**2)
			r_list.append(d_to_object)
			xy_list.append([self.bgr_target.x, self.bgr_target.y,d_to_object])


		# find the closest object that is inside the area of operation
			# Finally return
		###################TODO modified and need to be checked #########################################
		if (len(xy_list)>0):
			xy_list_sorted=[]
			for i in range(0,len(xy_list)):
				xy_list_sorted.append([xy_list[i][0],xy_list[i][1],r_list[i]])
			xy_list_sorted=sorted(xy_list, key=self.getThirdElemt)
			for i in range(0,len(xy_list_sorted)):
				bodyRot = self.bodK.yaw - pi/2.0
				x_enu =  xy_list_sorted[i][1]*cos(bodyRot) - xy_list_sorted[i][0]*sin(bodyRot)
				y_enu = xy_list_sorted[i][1]*sin(bodyRot) + xy_list_sorted[i][0]*cos(bodyRot)
				dx_enu = x_enu
				dy_enu = y_enu
				##x and y switched because this function operates in NED frame
				[lat_object,lon_object]=self.local_deltaxy_LLA(self.current_lat, self.current_lon,  dy_enu,  dx_enu)
				if( self.quad_op_area.is_inside([lat_object,lon_object]) ):
					objectFound=True
					return (objectFound, [xy_list_sorted[i][0],xy_list_sorted[i][1]])
				else:
					print("Object seen but neglected")
					objectFound = False
					return (objectFound, [])
		else:
			objectFound=False
			return (objectFound, [])
		############################################################################

	########## End of Monitoring Single Object  #######################


	# determins if an object is inside an allowable descend envelope
	def inside_envelope(self,xy):
		# currently, only based on relative pos
		if np.sqrt(xy[0]**2 + xy[1]**2) <= self.ENVELOPE_XY_POS:
			return True
		else:
			return False
	############## End of envelope function #################

	######## for debug: prints the state/signal
	def debug(self):
		if self.DEBUG:
			print '#--------------------------------------#'
			print 'State/Signal: ', self.current_state, ' -> ', self.current_signal
			print '#--------------------------------------#'

			if rospy.is_shutdown():
				print '|------ ROS IS SHUTTING DOWN------|'
	############### End of debug function ##################

	######## for intermediate points along a great circle path:########################

		#The function takes two coordinate pairs and a user-specified number of segments.
		#It yields a set of intermediate points along a great circle path.
		#def tweensegs(longitude1,latitude1,longitude2,latitude2,num_of_segments):

	def intermediate(self,llaPointStart,llaPointEnd,num_of_segments):

		ptlon1 = llaPointStart[1]
		ptlat1 = llaPointStart[0]
		ptlon2 = llaPointEnd[1]
		ptlat2 = llaPointEnd[0]

		numberofsegments = num_of_segments
		onelessthansegments = numberofsegments - 1
		fractionalincrement = (1.0/onelessthansegments)

		ptlon1_radians = radians(ptlon1)
		ptlat1_radians = radians(ptlat1)
		ptlon2_radians = radians(ptlon2)
		ptlat2_radians = radians(ptlat2)

		distance_radians=2*asin(sqrt(pow((sin((ptlat1_radians-ptlat2_radians)/2)),2) + cos(ptlat1_radians)*cos(ptlat2_radians)*pow((sin((ptlon1_radians-ptlon2_radians)/2)),2)))
		# 6371.009 represents the mean radius of the earth
		# shortest path distance
		distance_km = 6371.009 * distance_radians

		mylats = []
		mylons = []

		# write the starting coordinates
		mylats.append([])
		mylons.append([])
		mylats[0] = ptlat1
		mylons[0] = ptlon1

		f = fractionalincrement
		icounter = 1
		while (icounter <  onelessthansegments):
			icountmin1 = icounter - 1
			mylats.append([])
			mylons.append([])
			# f is expressed as a fraction along the route from point 1 to point 2
			A=sin((1-f)*distance_radians)/sin(distance_radians)
			B=sin(f*distance_radians)/sin(distance_radians)
			x = A*cos(ptlat1_radians)*cos(ptlon1_radians) + B*cos(ptlat2_radians)*cos(ptlon2_radians)
			y = A*cos(ptlat1_radians)*sin(ptlon1_radians) +  B*cos(ptlat2_radians)*sin(ptlon2_radians)
			z = A*sin(ptlat1_radians) + B*sin(ptlat2_radians)
			newlat=atan2(z,sqrt(pow(x,2)+pow(y,2)))
			newlon=atan2(y,x)
			newlat_degrees = degrees(newlat)
			newlon_degrees = degrees(newlon)
			mylats[icounter] = newlat_degrees
			mylons[icounter] = newlon_degrees
			icounter += 1
			f = f + fractionalincrement

		# write the ending coordinates
		mylats.append([])
		mylons.append([])
		mylats[onelessthansegments] = ptlat2
		mylons[onelessthansegments] = ptlon2
		listOfPoints=[]
		for i in range(0,numberofsegments):
			listOfPoints.append([mylats[i],mylons[i]])
		return listOfPoints

		# Now, the array mylats[] and mylons[] have the coordinate pairs for intermediate points along the geodesic
		# My mylat[0],mylat[0] and mylat[num_of_segments-1],mylat[num_of_segments-1] are the geodesic end point
        ############### End of intermediate function ##################

	######## function for defining the field boundaries :########################
	def path(self):
		if (self.namespace=="/Quad1"):

			num_of_segments_up_1=max(int(7/self.cameraView),3)
			upperBoundaries_1=self.intermediate(self.areaBoundaries[1],self.areaBoundaries[0],num_of_segments_up_1)

			num_of_segments_up_2=max(int(18/self.cameraView),4)
			upperBoundaries_2=self.intermediate(self.areaBoundaries[0],self.areaBoundaries[7],num_of_segments_up_2)
			del upperBoundaries_2[0]

			num_of_segments_up_3=max(int(5/self.cameraView),3)
			upperBoundaries_3=self.intermediate(self.areaBoundaries[8],self.areaBoundaries[9],num_of_segments_up_3)
			del upperBoundaries_3[-1]

			upperBoundaries=upperBoundaries_1+upperBoundaries_2+upperBoundaries_3

			num_of_segments_down=len(upperBoundaries)+1
			downBoundaries=self.intermediate(self.areaBoundaries[2],self.areaBoundaries[3],num_of_segments_down)
			del downBoundaries[-1]


			way_points_list=[]
			for i in range(0,len(upperBoundaries)-1):
				way_points_list.append(downBoundaries[i])
				way_points_list.append(upperBoundaries[i])
		if (self.namespace=="/Quad2"):
			num_of_segments_up_1=max(int(5/self.cameraView),3)
			upperBoundaries_1=self.intermediate(self.areaBoundaries[9],self.areaBoundaries[10],num_of_segments_up_1)
			del upperBoundaries_1[0]

			num_of_segments_up_2=max(int(18/self.cameraView),4)
			upperBoundaries_2=self.intermediate(self.areaBoundaries[11],self.areaBoundaries[6],num_of_segments_up_2)
			del upperBoundaries_2[-1]

			num_of_segments_up_3=max(int(7/self.cameraView),3)
			upperBoundaries_3=self.intermediate(self.areaBoundaries[6],self.areaBoundaries[5],num_of_segments_up_3)
			upperBoundaries=upperBoundaries_1+upperBoundaries_2+upperBoundaries_3

			num_of_segments_down=len(upperBoundaries)+1
			downBoundaries=self.intermediate(self.areaBoundaries[3],self.areaBoundaries[4],num_of_segments_down)
			del downBoundaries[0]


			way_points_list=[]
			for i in range(0,len(downBoundaries)-1):
				way_points_list.append(downBoundaries[i])
				way_points_list.append(upperBoundaries[i])

		if (self.namespace=="/Quad3"):

			num_of_segments=int(46/self.cameraView)

			upperBoundaries=self.intermediate(self.areaBoundaries[0],self.areaBoundaries[6],num_of_segments)
			del upperBoundaries[0]
			downBoundaries=self.intermediate(self.areaBoundaries[7],self.areaBoundaries[11],num_of_segments)
			del downBoundaries[0]
			way_points_list=[]
			for i in range(0,len(downBoundaries)-1):
				way_points_list.append(upperBoundaries[i])
				way_points_list.append(downBoundaries[i])


		return(way_points_list)

	######## function for converting LLA points to local delta xy(NED) :########################
	def LLA_local_deltaxy(self, lat_0, lon_0,  lat,  lon):

		M_DEG_TO_RAD = 0.01745329251994
		CONSTANTS_RADIUS_OF_EARTH	= 6371000.0
		DBL_EPSILON = 2.2204460492503131E-16

		curr_lat_rad = lat_0 * M_DEG_TO_RAD
		curr_lon_rad = lon_0 * M_DEG_TO_RAD
		curr_sin_lat = sin(curr_lat_rad)
		curr_cos_lat = cos(curr_lat_rad)

		lat_rad = lat * M_DEG_TO_RAD
		lon_rad = lon * M_DEG_TO_RAD

		sin_lat = sin(lat_rad)
		cos_lat = cos(lat_rad)

		cos_d_lon = cos(lon_rad - curr_lon_rad)

		arg = curr_sin_lat * sin_lat + curr_cos_lat * cos_lat * cos_d_lon

		if (arg > 1.0):
			arg = 1.0
		elif (arg < -1.0):
			arg = -1.0

		c = acos(arg)

		if(abs(c) < DBL_EPSILON):
			k=1
		else:
			k=c/sin(c)


		delta_x = k * (curr_cos_lat * sin_lat - curr_sin_lat * cos_lat * cos_d_lon) * CONSTANTS_RADIUS_OF_EARTH
		delta_y = k * cos_lat * sin(lon_rad - curr_lon_rad) * CONSTANTS_RADIUS_OF_EARTH
		return (delta_x,delta_y)

	############### End of LLA_local_deltaxy function ##################

	########function for getting the third element of a list ###########
	def getThirdElemt(self, item):
		return item[2]
	####################################################################

	######## function for converting local delta xy(NED) to LLA points :########################
	def local_deltaxy_LLA(self,lat_0, lon_0,  delta_x,  delta_y):

		M_DEG_TO_RAD = 0.01745329251994
		CONSTANTS_RADIUS_OF_EARTH	= 6371000.0
		DBL_EPSILON = 2.2204460492503131E-16

		curr_lat_rad = lat_0 * M_DEG_TO_RAD
		curr_lon_rad = lon_0 * M_DEG_TO_RAD
		curr_sin_lat = sin(curr_lat_rad)
		curr_cos_lat = cos(curr_lat_rad)

		x_rad = delta_x / CONSTANTS_RADIUS_OF_EARTH
		y_rad = delta_y / CONSTANTS_RADIUS_OF_EARTH
		c = sqrt(x_rad * x_rad + y_rad * y_rad)
		sin_c = sin(c)
		cos_c = cos(c)

		if (fabs(c) > DBL_EPSILON):
			lat_rad = asin(cos_c * curr_sin_lat + (x_rad * sin_c * curr_cos_lat) / c)
			lon_rad = (curr_lon_rad + atan2(y_rad * sin_c, c * curr_cos_lat * cos_c - x_rad * curr_sin_lat * sin_c))

		else:
			lat_rad = curr_lat_rad
			lon_rad = curr_lon_rad

		lat = lat_rad * 180.0 / pi
		lon = lon_rad * 180.0 / pi
		return(lat,lon)
	############### End of local_deltaxy_LLA function ##################

	################ test gripper actuation ##########################
     	def test_gripper(self, cmd):
        	# activate/deactivate gripper
        	self.gripper_action.data = cmd
        	self.gripper_pub.publish(self.gripper_action)
        	# print gripper status
        	print '#---------------------------------#'
        	print 'Gripper status: ', self.gripperIsPicked
        	print '#---------------------------------#'
	############### End of test_gripper #############################

#                                           (End of helper functions)                                                                    #
#----------------------------------------------------------------------------------------------------------------------------------------#

#----------------------------------------------------------------------------------------------------------------------------------------#
#                                                 (Callbacks)                                                                            #

	############ Gripper callback function #####################
	def gripper_cb(self,msg):
		if msg is not None:
			self.gripperIsPicked = msg.data
	########### End of Gripper callback function ##############

	#################### MAVROS GPS Callback #################
	def gps_cb(self, msg):
		if msg is not None:
			self.current_lat = msg.latitude
			self.current_lon = msg.longitude
	################## End of GPS callback ##################

	#################### MAVROS other GPS Callback #################
	def gps_other_1_cb(self, msg):
		if msg is not None:
			self.other_1_current_lat = msg.latitude
			self.other_1_current_lon = msg.longitude

	def gps_other_2_cb(self, msg):
		if msg is not None:
			self.other_2_current_lat = msg.latitude
			self.other_2_current_lon = msg.longitude
	################## End of other GPS callback ##################
	#################### MAVROS others states Callback #################
	def state_other_1_cb(self, msg):
		if msg is not None:
			self.other_1_state = msg.state

	def state_other_2_cb(self, msg):
		if msg is not None:
			self.other_2_state = msg.state
	################## End of other others states callback ##################

#                                              (End of Callbacks)                                                                        #
#----------------------------------------------------------------------------------------------------------------------------------------#

#############################################
#          Mission function             #
def mission():
	rospy.init_node('mission1', anonymous=True)

	# get namespace
	ns=rospy.get_namespace()
	ns = ns[0:len(ns)-1]

	field_map=[]
    # read field_map from a YAML config file as a ros parameter
    # check if the field_map parameter is set
	if rospy.has_param(ns+'/field_map'):
        	field_map = rospy.get_param(ns+'/field_map')

	print 'Length of field map= ', len(field_map)

	if len(field_map) < 14:
		print 'Field map is not set properly. Exiting.....'
		return

	sm = StateMachineC(ns,field_map)
	sm.DEBUG=True
	sm.TKOFFALT = 5.0
	sm.current_state='Start'
	sm.current_signal='Ready'
	sm.START_SIGNAL=True
	sm.cameraView=1

	while not rospy.is_shutdown():
		sm.update_state()


######### Main ##################
if __name__ == '__main__':
    try:
        mission()
    except rospy.ROSInterruptException:
        pass
