#!/usr/bin/env python

import rospy
import numpy as np
import tf

from math import *
from std_msgs.msg import *
from sensor_msgs.msg import *
from geometry_msgs.msg import *
from mavros_msgs.msg import *
from mavros_msgs.srv import *

import autopilotLib
import myLib
import autopilotParams

# Main loop

def autopilot():
    rospy.init_node('autopilot', anonymous=True)
    # get namespace
    ns=rospy.get_namespace()
    autopilotParams.setParams(ns)
    ###################################
    # Publishers and parameters
    command = rospy.Publisher('/mavros/setpoint_raw/local', PositionTarget, queue_size=10)
    ###################################
    # Instantiate a setpoint
    setp = PositionTarget()
    setp.type_mask = int('010111000111', 2)

    # Instantiate altitude controller
    altK = autopilotLib.kAltVel(ns)

    # Instantiate body controller
    bodK = autopilotLib.kBodVel(ns)

    # Establish a rate
    fbRate = rospy.get_param('/autopilot/fbRate')
    rate = rospy.Rate(fbRate)

    # Cycle to register local position
    kc = 0.0
    while kc < 10: # cycle for subscribers to read local position
        rate.sleep()
        kc = kc + 1

    zGround = altK.z

    #####
    # Execute altitude step response while holding current position
    #####

    altK.zSp = zGround + rospy.get_param('/autopilot/altStep')
    home = autopilotLib.xyzVar()
    home.x = bodK.x
    home.y = bodK.y

    while not abs(altK.zSp - altK.z) < 0.2 and not rospy.is_shutdown():
        
        setp.header.stamp = rospy.Time.now()

        setp.velocity.z = altK.controller()
        (bodK.xSp,bodK.ySp) = autopilotLib.wayHome(bodK,home)
        (setp.velocity.x,setp.velocity.y,setp.yaw_rate) = bodK.controller()

        rate.sleep()
        command.publish(setp)
        
        print 'Set/Alt/Gnd:',altK.zSp, altK.z, zGround
        
        
    #####
    # Track circling trajectory
    #####
    
    V = 4.0
    omega = 0.1
    theta = pi/2.0
    home.x = 10.0
    home.y = 15.0
    
    while not rospy.is_shutdown():
    
        setp.header.stamp = rospy.Time.now()
            
        home.x = home.x + (1.0/fbRate)*V*cos(theta)
        home.y = home.y + (1.0/fbRate)*V*sin(theta)
        theta = theta + (1.0/fbRate)*omega
        
        setp.velocity.z = altK.controller()
        (bodK.xSp,bodK.ySp) = autopilotLib.wayHome(bodK,home)
        (setp.velocity.x,setp.velocity.y,setp.yaw_rate) = bodK.controller()

        rate.sleep()
        command.publish(setp)
        
        bodK.ekfUpdate()
        
        error = sqrt((home.x - bodK.x)**2 + (home.y - bodK.y)**2)
        print "err/yawR/wHat/vHat: ", error, setp.yaw_rate, np.asscalar(bodK.ekf.xhat[4]), np.asscalar(bodK.ekf.xhat[3])

        
if __name__ == '__main__':
    try:
        autopilot()
    except rospy.ROSInterruptException:
        pass





 
